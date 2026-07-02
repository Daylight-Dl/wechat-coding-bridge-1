# -*- coding: utf-8 -*-
"""
WeChat <-> Claude Code 轻量桥 (异步版)。
iLink getupdates(收) -> 队列 -> 后台 worker: claude -p(大脑) -> iLink sendmessage(回)

架构(根治"连发就卡死/丢消息"):
- 生产者(主循环): 持续 getupdates -> 去重/主人校验/ctx -> /reset 内联 -> 立刻回执 -> 入队 -> 继续轮询。永不被处理阻塞。
- 消费者(后台单 worker 线程): 串行 receive_message(媒体下载解密) + run_claude + 回复。串行保会话连续性, 不让并行 claude --resume 串台。
- 所有子进程统一 run_proc: 硬超时 + 超时 taskkill /F /T 杀整棵进程树(防卡死/僵尸 claude)。
中文通过 claude 的进程参数传递(Windows CreateProcessW 宽字符), 不乱码。

配置: 同目录 config.json(可选), 字段 claude_exe / work_dir / effort 全部可缺省, 见 config.example.json。
"""
import shutil
import json
import os
import sys
import time
import datetime
import subprocess
import secrets
import base64
import re
import shutil
import threading
import queue
import urllib.request

HOME = os.path.expanduser("~")
DATA_DIR = os.path.join(HOME, ".cli-bridge")
CRED_FILE = os.path.join(DATA_DIR, "account.json")
CONTEXT_CACHE_FILE = os.path.join(DATA_DIR, "context_tokens.json")

PROJ_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.path.join(PROJ_DIR, "session.json")
SEND_MEDIA_JS = os.path.join(PROJ_DIR, "send_media.mjs")
RECV_MEDIA_JS = os.path.join(PROJ_DIR, "recv_media.mjs")
TMP_DIR = os.path.join(PROJ_DIR, ".tmp")

# D盘日志目录
LOG_DIR = r"D:\wechat_bridge_logs"
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)
LOG_FILE = os.path.join(LOG_DIR, "wechat_bridge.log")
# 发送本地文件/图片的指令: 模型在回复里写 [[SENDFILE:路径]] / [[SENDFILE:路径|显示名]] / [[SENDIMAGE:路径|配文]]
MEDIA_RE = re.compile(r"\[\[SEND(FILE|IMAGE):([^\]]+)\]\]")

CHANNEL_VERSION = "0.3.0"


def _load_config():
    """读取本脚本同目录的 config.json(可选)。所有字段都可缺省, 见 config.example.json。"""
    cfg_path = os.path.join(PROJ_DIR, "config.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}
    return {}


_CFG = _load_config()


def _resolve_claude_exe(cfg_val):
    """找一个能被 subprocess 直接执行的 claude。
    Windows 上 npm 的 claude.cmd / 无扩展 shim 不能被 Popen 直接跑(且经 cmd.exe 传中文会乱码),
    所以去 node_modules 找真正的 claude.exe; 非 Windows 上 which 找到的可直接执行。"""
    if cfg_val:
        return cfg_val
    found = shutil.which("claude")
    if os.name == "nt":
        if found:
            real = os.path.join(os.path.dirname(found), "node_modules",
                                "@anthropic-ai", "claude-code", "bin", "claude.exe")
            if os.path.exists(real):
                return real
        exe = shutil.which("claude.exe")
        if exe:
            return exe
    return found or "claude"


# claude 可执行文件(Windows 解析到真正的 .exe, 避免 .cmd 执行失败/中文乱码)
CLAUDE_EXE = _resolve_claude_exe(_CFG.get("claude_exe"))
# claude 工作目录(放着你自己的 CLAUDE.md / 记忆); 缺省用当前用户主目录
CWD = _CFG.get("work_dir") or HOME
# 思考强度 low|medium|high|xhigh|max(缺省 high), 仅 claude 适配器用
EFFORT = _CFG.get("effort", "high")

# 大脑适配器: claude(默认/功能最全) | codex | custom(任意"一句话出答案"的 CLI)
ADAPTER = (_CFG.get("adapter") or "claude").strip().lower()
CODEX_EXE = _CFG.get("codex_exe") or shutil.which("codex") or "codex"
CODEX_EXEC_ARGS = _CFG.get("codex_exec_args") or [
    "--full-auto", "--skip-git-repo-check"]
_CUSTOM = _CFG.get("custom") or {}

# 子进程硬超时(秒)
CLAUDE_TIMEOUT = 600
RECV_TIMEOUT = 180
SEND_TIMEOUT = 300

BRIDGE_PROMPT = (
    "你正在通过【微信】和你的主人对话, 你是 TA 的私人 AI 助手。"
    "你这一整段回复会被原样作为一条微信消息发出去, 所以: 用口语化、简洁的话; "
    "不要用复杂 Markdown 表格或大段代码块(微信里显示不好), 要分点就用简单的 1. 2. 3.。"
    "【发文件能力】如果主人要你把本地文件或图片(PDF/文档/图片等)发到微信, "
    "在回复里单独加一行指令: 发文件用 [[SENDFILE:绝对路径]] 或 [[SENDFILE:绝对路径|显示文件名]]; "
    "发图片用 [[SENDIMAGE:绝对路径]] 或 [[SENDIMAGE:绝对路径|配文]]。桥会自动把文件发出去并把这行指令从消息里删掉。"
    "只在确认文件真实存在时用; 图片≤20MB、文件≤50MB。"
)

# Windows: spawn 子进程(claude/node)时不弹出黑色控制台窗口(配合 pythonw 静默后台运行)
CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# pythonw 无控制台运行时 sys.stdout/stderr 为 None, print() 会抛错拖垮桥进程。
# stdout 丢弃(log() 自己写文件, 避免日志重复); stderr 接到日志文件以捕获未处理异常回溯。
if sys.stdout is None or sys.stderr is None:
    try:
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        if sys.stderr is None:
            sys.stderr = open(LOG_FILE, "a", encoding="utf-8")
    except Exception:
        pass

# 跨线程共享
WORK_Q = queue.Queue()          # 生产者塞 (account, msg, frm, use_ctx); worker 取
LATEST_CTX = {}                 # frm -> 最新 context_token (生产者写, worker 读)
_LOG_LOCK = threading.Lock()    # 多线程写日志不串行


def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[%s] %s" % (ts, msg)
    with _LOG_LOCK:
        try:
            print(line, flush=True)
        except Exception:
            pass
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def _maybe_cmd_wrap(args):
    """Windows: npm 的 .cmd/.bat shim 不能被 Popen 直接执行, 用 cmd /c 包一层。"""
    if os.name == "nt" and args and isinstance(args[0], str) and args[0].lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c"] + list(args)
    return args


def run_proc(args, cwd, timeout, stdin_text=None):
    """统一跑子进程。返回 (status, stdout, stderr)。status: ok|timeout|notfound|error。
    超时则 taskkill /F /T 杀整棵进程树(防卡死/僵尸)。stdin_text 非 None 则喂给子进程标准输入。"""
    eff = _maybe_cmd_wrap(args)
    try:
        p = subprocess.Popen(eff, cwd=cwd,
                             stdin=(
                                 subprocess.PIPE if stdin_text is not None else None),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
    except FileNotFoundError:
        return "notfound", "", ""
    except Exception as e:
        return "error", "", str(e)
    try:
        out, err = p.communicate(input=stdin_text, timeout=timeout)
        return "ok", out or "", err or ""
    except subprocess.TimeoutExpired:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=15)
        except Exception:
            pass
        try:
            p.kill()
        except Exception:
            pass
        try:
            out, err = p.communicate(timeout=10)
        except Exception:
            out, err = "", ""
        return "timeout", out or "", err or ""


def load_account():
    if not os.path.exists(CRED_FILE):
        return {}
    with open(CRED_FILE, encoding="utf-8") as f:
        return json.load(f)


def rand_uin():
    n = int.from_bytes(secrets.token_bytes(4), "big")
    return base64.b64encode(str(n).encode()).decode()


def api(account, endpoint, body_obj, timeout=40):
    base = account["baseUrl"]
    if not base.endswith("/"):
        base += "/"
    body = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": rand_uin(),
        "Authorization": "Bearer " + account["token"],
        "Content-Length": str(len(body)),
    }
    req = urllib.request.Request(
        base + endpoint, data=body, headers=headers, method="POST")
    # 每次新建 opener: 直连绕代理 + 多线程安全(生产者长轮询与 worker 发送互不污染)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def load_session():
    """返回会话状态 dict(各适配器自己解释字段); 没有则空 dict。"""
    try:
        with open(SESSION_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_session(d):
    try:
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass


def clear_session():
    try:
        if os.path.exists(SESSION_FILE):
            os.remove(SESSION_FILE)
    except Exception:
        pass


def _persona_prefix(text):
    """非 claude 适配器没有 --append-system-prompt, 新会话第一条消息前注入微信人设。"""
    return BRIDGE_PROMPT + "\n\n----\n用户消息: " + text


def run_brain(text):
    """按 config.json 的 adapter 选 coding 工具当大脑, 返回最终回复文本。"""
    if ADAPTER == "codex":
        return run_codex(text)
    if ADAPTER == "custom":
        return run_custom(text)
    return run_claude(text)


def run_claude(text):
    """Claude Code: claude -p, stream-json 解析, --resume 续接。中文走宽字符参数不乱码, 硬超时杀树。"""
    sess = load_session()
    sid = sess.get("session_id") if sess.get(
        "adapter", "claude") == "claude" else None
    args = [CLAUDE_EXE, "-p", text,
            "--output-format", "stream-json", "--verbose",
            "--permission-mode", "bypassPermissions",
            "--strict-mcp-config",  # 私聊用不到 MCP, 关掉省 init 时间(没配 MCP 的话留着也无妨)
            "--effort", EFFORT,     # 思考强度(从 config.json 读, 缺省 high)
            "--append-system-prompt", BRIDGE_PROMPT]
    if sid:
        args += ["--resume", sid]
    log("run claude (resume=%s): %s" % (bool(sid), text[:40]))
    status, out, err = run_proc(args, CWD, CLAUDE_TIMEOUT)
    if status == "notfound":
        log("claude 未找到: %s" % CLAUDE_EXE)
        return "(找不到 claude 程序, 检查 config.json 的 claude_exe 或系统 PATH)"
    if status == "timeout":
        log("claude 超时(已杀进程树)")
        return "(想太久超时了, 请再说一次, 或发 /reset 重开)"
    if status == "error":
        log("claude 启动失败: %s" % err[:160])
        return "(启动失败, 请再试一次, 或发 /reset)"
    result_text, new_sid = None, None
    for line in (out or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            new_sid = ev.get("session_id") or new_sid
        if ev.get("type") == "result":
            result_text = ev.get("result")
            new_sid = ev.get("session_id") or new_sid
    if new_sid:
        save_session({"adapter": "claude", "session_id": new_sid})
    if not result_text:
        log("claude 无结果. stderr=%s" % (err or "")[:200])
        return "(这次没出结果, 请再试一次, 或发 /reset)"
    return result_text


def run_codex(text):
    """Codex CLI(headless): 新会话 `codex exec` / 续接 `codex exec resume --last`。
    最终回复用 `-o <文件>` 取(干净的最后一条消息), 不依赖解析 JSON 事件格式。"""
    sess = load_session()
    has_session = sess.get("adapter") == "codex" and bool(
        sess.get("codex_started"))
    os.makedirs(TMP_DIR, exist_ok=True)
    last_file = os.path.join(TMP_DIR, "codex_last_%s.txt" %
                             secrets.token_hex(4))
    # 末尾 '-' = 提问从 stdin 读(中文不经命令行, 不乱码)
    tail = list(CODEX_EXEC_ARGS) + ["-C", CWD, "-o", last_file, "-"]
    if has_session:
        args = [CODEX_EXE, "exec", "resume", "--last"] + tail
        stdin_text = text
    else:
        args = [CODEX_EXE, "exec"] + tail
        stdin_text = _persona_prefix(text)  # 新会话注入人设
    log("run codex (resume=%s): %s" % (has_session, text[:40]))
    status, out, err = run_proc(
        args, CWD, CLAUDE_TIMEOUT, stdin_text=stdin_text)
    if status == "notfound":
        log("codex 未找到: %s" % CODEX_EXE)
        return "(找不到 codex 程序, 检查 config.json 的 codex_exe 或系统 PATH)"
    if status == "timeout":
        log("codex 超时(已杀进程树)")
        return "(想太久超时了, 请再说一次, 或发 /reset 重开)"
    reply = ""
    try:
        if os.path.exists(last_file):
            with open(last_file, encoding="utf-8", errors="replace") as f:
                reply = f.read().strip()
    except Exception as e:
        log("读 codex 输出失败: %s" % e)
    finally:
        try:
            os.remove(last_file)
        except Exception:
            pass
    if not reply:
        log("codex 无结果(status=%s). stderr=%s" % (status, (err or "")[:200]))
        return "(这次没出结果, 请再试一次, 或发 /reset)"
    save_session({"adapter": "codex", "codex_started": True})
    return reply


def run_custom(text):
    """通用适配器: 调任意"一句话出答案"的 coding CLI, 取 stdout 当回复(无会话续接)。
    config.json 配 custom.cmd 和 custom.args(用 {prompt} 占位)。每条消息都带上人设前缀。"""
    cmd = _CUSTOM.get("cmd")
    if not cmd:
        return "(未配置 custom.cmd, 请在 config.json 的 custom 里填你的 coding 工具命令)"
    raw_args = _CUSTOM.get("args") or ["{prompt}"]
    prompt = _persona_prefix(text)
    args = [cmd] + [(a.replace("{prompt}", prompt)
                     if isinstance(a, str) else str(a)) for a in raw_args]
    log("run custom (%s): %s" % (cmd, text[:40]))
    status, out, err = run_proc(args, CWD, CLAUDE_TIMEOUT)
    if status == "notfound":
        return "(找不到 %s 程序, 检查 config.json 的 custom.cmd 或系统 PATH)" % cmd
    if status == "timeout":
        return "(想太久超时了, 请再说一次, 或发 /reset 重开)"
    reply = (out or "").strip()
    if not reply:
        log("custom 无结果(status=%s). stderr=%s" % (status, (err or "")[:200]))
        return "(这次没出结果, 请再试一次, 或发 /reset)"
    return reply


def send_text(account, to_user_id, context_token, text):
    body = {
        "msg": {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": "wxq:%s-%s" % (datetime.datetime.now().strftime("%H%M%S"), secrets.token_hex(3)),
            "message_type": 2,        # BOT
            "message_state": 2,       # FINISH
            "item_list": [{"type": 1, "text_item": {"text": text}}],
            "context_token": context_token,
        },
        "base_info": {"channel_version": CHANNEL_VERSION},
    }
    return api(account, "ilink/bot/sendmessage", body, timeout=20)


def persist_context(frm, ctx):
    """把最新上下文token落盘到 context_tokens.json, 供 send_media.mjs 发文件时用(保持新鲜防过期)。"""
    if not (frm and ctx):
        return
    try:
        data = {}
        if os.path.exists(CONTEXT_CACHE_FILE):
            with open(CONTEXT_CACHE_FILE, encoding="utf-8") as f:
                data = json.load(f) or {}
        if data.get(frm) != ctx:
            data[frm] = ctx
            with open(CONTEXT_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log("persist ctx err: %s" % e)


def send_media(kind, file_path, to_user_id, label=None):
    """调 Node 助手发文件/图片。kind=FILE|IMAGE。返回是否成功。硬超时杀树。"""
    action = "image" if kind == "IMAGE" else "file"
    args = ["node", SEND_MEDIA_JS, action,
            file_path, to_user_id or "", (label or "")]
    status, out, err = run_proc(args, PROJ_DIR, SEND_TIMEOUT)
    sout = (out or "").strip()
    ok = '"ok":true' in sout
    log("send_media %s '%s' -> %s" % (action, os.path.basename(file_path),
                                      (sout or err or status)[:160]))
    return ok


def split_media_directives(reply):
    """从回复里抽出发文件/图片指令, 返回(去掉指令后的纯文本, [(kind, path, label), ...])。"""
    medias = []

    def repl(m):
        kind = m.group(1)
        body = m.group(2).strip()
        if "|" in body:
            path_part, label = body.split("|", 1)
            medias.append((kind, path_part.strip(), label.strip()))
        else:
            medias.append((kind, body, None))
        return ""

    text = MEDIA_RE.sub(repl, reply).strip()
    return text, medias


def extract_text(msg):
    parts = []
    for item in msg.get("item_list") or []:
        if item.get("type") == 1:
            t = (item.get("text_item") or {}).get("text")
            if t:
                parts.append(t.strip())
    return "\n".join(parts).strip()


def has_media_items(msg):
    """是否含图片(2)/语音(3)/文件(4)/视频(5)等非纯文本项。"""
    for item in msg.get("item_list") or []:
        if item.get("type") in (2, 3, 4, 5):
            return True
    return False


def receive_message(msg):
    """返回要发给 claude 的文本。纯文本直接返回; 含图片/语音/文件则下载解密并把本地路径标注进文本。硬超时杀树。"""
    if not has_media_items(msg):
        return extract_text(msg)
    try:
        os.makedirs(TMP_DIR, exist_ok=True)
        tmp = os.path.join(TMP_DIR, "inmsg_%s.json" % secrets.token_hex(4))
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(msg, f, ensure_ascii=False)
        status, out, err = run_proc(
            ["node", RECV_MEDIA_JS, tmp], PROJ_DIR, RECV_TIMEOUT)
        try:
            os.remove(tmp)
        except Exception:
            pass
        data = None
        for line in (out or "").splitlines():
            line = line.strip()
            if line.startswith("{"):
                data = json.loads(line)
        if not data or not data.get("ok"):
            log("recv_media 失败(%s): %s" % (status, (out or err or "")[:160]))
            return extract_text(msg) or "(收到一条带附件的消息, 但附件下载失败)"
        parts = []
        if data.get("text"):
            parts.append(data["text"])
        for a in data.get("attachments") or []:
            kind = {"image": "图片", "file": "文件"}.get(
                a.get("kind"), a.get("kind") or "附件")
            parts.append("[主人通过微信发来一个%s, 已存到本地: %s (用 Read 工具查看内容)]"
                         % (kind, a.get("path")))
        log("收到媒体: %d 个附件" % len(data.get("attachments") or []))
        return "\n".join(parts).strip()
    except Exception as e:
        log("receive_message err: %s" % e)
        return extract_text(msg)


# ============ 消费者: 后台 worker, 串行处理 ============

def process_one(account, msg, frm, use_ctx):
    text = receive_message(msg)   # 媒体下载解密(带硬超时)
    if not text:
        log("空内容, 跳过 claude")
        return
    reply = run_brain(text)
    send_ctx = LATEST_CTX.get(frm) or use_ctx
    clean_text, medias = split_media_directives(reply)
    if clean_text:
        try:
            send_text(account, frm, send_ctx, clean_text)
            log("已回复 %d 字" % len(clean_text))
        except Exception as e:
            log("send reply err: %s" % str(e)[:140])
    for kind, fpath, label in medias:
        try:
            if send_media(kind, fpath, frm, label):
                log("已发%s: %s" % ("图片" if kind ==
                    "IMAGE" else "文件", os.path.basename(fpath)))
            else:
                send_text(account, frm, send_ctx,
                          "(发送文件失败: %s, 看下路径或大小是否超限)" % os.path.basename(fpath))
        except Exception as e:
            log("send media err: %s" % str(e)[:140])


def worker():
    while True:
        account, msg, frm, use_ctx = WORK_Q.get()
        try:
            process_one(account, msg, frm, use_ctx)
        except Exception as e:
            log("worker err: %s" % str(e)[:160])
            try:
                send_ctx = LATEST_CTX.get(frm) or use_ctx
                send_text(account, frm, send_ctx, "(处理这条时出错了, 请再说一次或发 /reset)")
            except Exception:
                pass
        finally:
            WORK_Q.task_done()


# ============ 生产者: 主循环, 只收+回执+入队, 永不阻塞 ============

def handle_inbound(account, owner, msg, seen, start_ms):
    if msg.get("message_type") != 1:   # 只处理用户消息
        return
    frm = msg.get("from_user_id") or ""
    ctx = msg.get("context_token")
    cms = msg.get("create_time_ms") or 0
    key = "%s|%s|%s" % (frm, msg.get("client_id"), cms)
    if key in seen:
        return
    seen.add(key)
    if cms and cms < start_ms:   # 忽略历史积压
        return
    if owner and frm != owner:    # 只回主人
        log("非主人消息 from=%s, 忽略" % frm)
        return
    # 便宜预判(只看文本项 + 有无媒体), 不在主循环里做媒体下载
    text_preview = extract_text(msg)
    has_media = has_media_items(msg)
    if not text_preview and not has_media:
        return   # 空消息, 不回执不入队
    if ctx:
        LATEST_CTX[frm] = ctx
        persist_context(frm, ctx)   # 落盘最新token, 供发文件复用
    use_ctx = LATEST_CTX.get(frm) or ctx
    if not use_ctx:
        log("无 context_token, 跳过")
        return
    # /reset 由生产者立即处理(即便 worker 忙也能重置)
    if text_preview.strip() in ("/reset", "/new", "重置", "重新开始"):
        clear_session()
        try:
            send_text(account, frm, use_ctx, "已开始新对话(已清空上下文)。")
        except Exception as e:
            log("send err %s" % e)
        return
    log("收到: %s" % (text_preview[:60] if text_preview else "[媒体]"))
    # 立刻回执(让用户永远有反馈, 连发也不卡)
    try:
        ack = "🤔 收到, 正在想…" if not has_media else "🖼 收到图片/文件, 正在看…"
        send_text(account, frm, use_ctx, ack)
    except Exception as e:
        log("ack err %s" % e)
    # 入队, 交给后台 worker 串行处理
    WORK_Q.put((account, msg, frm, use_ctx))


def producer(account, owner):
        # 检查是否有 baseUrl
        if account is None or "baseUrl" not in account:
            print(f"❌ 错误：account 为空或缺失 'baseUrl' 字段。当前数据为: {account}")
            return

        # 这就是原来那行日志代码
        log("桥启动(异步), owner=%s base=%s cwd=%s" % (owner, account["baseUrl"], CWD))

        start_ms = time.time() * 1000 - 8000  # 8s 宽限; 忽略启动前的历史消息
        sync_buf = ""
        seen = set()
        while True:
            try:
                resp = api(account, "ilink/bot/getupdates", {"get_updates_buf": sync_buf, "base_info": {"channel_version": CHANNEL_VERSION}, "timeout": 40})
            except Exception as e:
                log("getupdates err: %s" % str(e)[:140])
                time.sleep(2)
                continue

            if resp.get("errcode") == -14 and "session timeout" in (resp.get("errmsg", "") or "").lower():
                log("sync session timeout, 清空游标")
                sync_buf = ""
                continue

            ret, errcode = resp.get("ret"), resp.get("errcode")
            if (ret is not None and ret != 0) or (errcode is not None and errcode != 0):
                log("getupdates bad: ret=%s errcode=%s msg=%s" % (ret, errcode, resp.get("errmsg")))
                time.sleep(2)
                continue

            if resp.get("get_updates_buf"):
                sync_buf = resp["get_updates_buf"]
                for m in (resp.get("msgs") or []):
                    handle_inbound(account, owner, m, seen, start_ms)


def main():
    account = load_account()
    if account is None:
        print("❌ 错误：account 为 None，微信登录或初始化失败！请检查是否扫码登录成功。")
        return
    owner = account.get("userId")
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    producer(account, owner)


def selftest():
    log("=== 自测: 直接调大脑(%s) ===" % ADAPTER)
    r = run_brain("你好, 一句话回答: 你是什么模型?")
    log("自测结果: %s" % r)
    try:
        print("\n=== SELFTEST RESULT ===\n" + r)
    except Exception:
        pass


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        selftest()
    else:
        main()
