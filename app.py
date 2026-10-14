"""
SSH Console — a Streamlit app to connect to any SSH server,
run commands in a terminal-style view and browse / preview / download / upload files over SFTP.

Deploy: push this folder to GitHub -> Streamlit Community Cloud -> main file: app.py
"""
import hmac
import html
import io
import posixpath
import shlex
import stat
import time
from datetime import datetime

import pandas as pd
import paramiko
import streamlit as st

st.set_page_config(page_title="SSH Console", page_icon="🖥️", layout="wide")

MARK = "__SSHCONSOLE_CWD__"
MAX_OUT = 200_000            # max bytes of command output kept
MAX_PREVIEW = 200_000        # max bytes previewed from a remote file
MAX_DOWNLOAD = 25 * 1024 * 1024
QUICK = [
    ("ls -la", "ls -la"),
    ("pwd", "pwd"),
    ("whoami", "whoami"),
    ("uptime", "uptime"),
    ("df -h", "df -h"),
    ("free -h", "free -h"),
    ("top (1 shot)", "top -b -n 1 | head -20"),
]
LANGS = {
    "py": "python", "js": "javascript", "ts": "typescript", "json": "json", "yml": "yaml",
    "yaml": "yaml", "sh": "bash", "bash": "bash", "md": "markdown", "html": "html",
    "css": "css", "sql": "sql", "xml": "xml", "toml": "toml", "c": "c", "cpp": "cpp",
    "java": "java", "go": "go", "rs": "rust", "ini": "ini", "log": None, "txt": None,
}

# ----------------------------------------------------------------------------- styling
CSS = """
<style>
.stApp{background:radial-gradient(1200px 600px at 10% -10%,#1b2447 0%,transparent 60%),radial-gradient(900px 500px at 100% 0%,#2a1a47 0%,transparent 55%),#0a0f1f;}
[data-testid="stSidebar"]{background:#0e1428;border-right:1px solid #1f2a4d;}
h1,h2,h3,label,p,span{color:#e6ebff;}
.hero{padding:34px 36px;border-radius:20px;border:1px solid #243061;background:linear-gradient(135deg,rgba(34,211,238,.10),rgba(167,139,250,.10));margin-bottom:22px;}
.hero h1{font-size:2.3rem;margin:0 0 6px 0;background:linear-gradient(90deg,#22d3ee,#a78bfa);-webkit-background-clip:text;-webkit-text-fill-color:transparent;}
.hero p{color:#aab4de;margin:0;font-size:1.02rem;}
.steps{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:20px;}
.step{background:#0f1730;border:1px solid #223060;border-radius:14px;padding:16px;color:#c9d2f5;font-size:.93rem;}
.step b{display:block;color:#22d3ee;margin-bottom:4px;}
.pill{display:inline-flex;align-items:center;gap:8px;padding:6px 14px;border-radius:999px;background:rgba(74,222,128,.12);border:1px solid rgba(74,222,128,.4);color:#86efac;font-size:.85rem;font-family:ui-monospace,Menlo,Consolas,monospace;}
.dot{width:8px;height:8px;border-radius:50%;background:#4ade80;box-shadow:0 0 8px #4ade80;}
.term{border:1px solid #243061;border-radius:14px;overflow:hidden;background:#070b17;box-shadow:0 10px 40px rgba(0,0,0,.45);margin-bottom:10px;}
.term-bar{display:flex;align-items:center;gap:7px;padding:9px 14px;background:#101834;border-bottom:1px solid #1d2850;color:#7f8bb8;font-size:.8rem;font-family:ui-monospace,Menlo,Consolas,monospace;}
.term-bar i{width:11px;height:11px;border-radius:50%;display:inline-block;}
.term-body{height:430px;overflow-y:auto;display:flex;flex-direction:column-reverse;padding:14px 16px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.86rem;line-height:1.45;}
.t-line{margin-top:10px;white-space:pre-wrap;word-break:break-all;}
.t-user{color:#4ade80;} .t-path{color:#60a5fa;} .t-sym{color:#e6ebff;margin:0 6px 0 3px;} .t-cmd{color:#f8fafc;font-weight:600;}
.t-out{margin:4px 0 0 0;color:#cbd5e1;white-space:pre-wrap;word-break:break-all;font:inherit;background:none;border:none;padding:0;}
.t-err{margin:4px 0 0 0;color:#fca5a5;white-space:pre-wrap;word-break:break-all;font:inherit;background:none;border:none;padding:0;}
.t-dim{color:#64709a;} .t-ok{color:#4ade80;font-size:.75rem;} .t-bad{color:#f87171;font-size:.75rem;}
[data-testid="stForm"]{border:none;padding:0;}
.stButton>button,.stDownloadButton>button,[data-testid="stFormSubmitButton"]>button{border-radius:10px;border:1px solid #2b3a73;background:#111a38;color:#dbe4ff;}
.stButton>button:hover,[data-testid="stFormSubmitButton"]>button:hover{border-color:#22d3ee;color:#22d3ee;}
</style>
"""


def esc(s: str) -> str:
    return html.escape(s or "")


# ----------------------------------------------------------------------------- helpers
def secret(name, default=None):
    try:
        return st.secrets[name]
    except Exception:
        return default


def init_state():
    defaults = dict(client=None, sftp=None, conn=None, cwd="/", path_input="/", history=[],
                    flash=None, info="", dl=None, up_n=0, authed=False)
    for k, v in defaults.items():
        st.session_state.setdefault(k, v)


def gate():
    pw = secret("APP_PASSWORD")
    if not pw or st.session_state.authed:
        return
    st.html('<div class="hero"><h1>SSH Console</h1><p>This app is password protected.</p></div>')
    with st.form("gate"):
        entered = st.text_input("Access password", type="password")
        if st.form_submit_button("Unlock"):
            if hmac.compare_digest(entered.encode(), str(pw).encode()):
                st.session_state.authed = True
                st.rerun()
            else:
                st.error("Wrong password.")
    st.stop()


def is_connected() -> bool:
    c = st.session_state.client
    if c is None:
        return False
    t = c.get_transport()
    return bool(t and t.is_active())


def human(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def load_pkey(text: str, passphrase: str):
    pw = passphrase or None
    for name in ("Ed25519Key", "ECDSAKey", "RSAKey", "DSSKey"):
        cls = getattr(paramiko, name, None)
        if cls is None:
            continue
        try:
            return cls.from_private_key(io.StringIO(text), password=pw)
        except paramiko.PasswordRequiredException:
            raise ValueError("This key is encrypted — enter its passphrase.")
        except Exception:
            continue
    raise ValueError("Could not read the private key (Ed25519, ECDSA and RSA are supported) or the passphrase is wrong.")


# ----------------------------------------------------------------------------- ssh core
def run_command(cmd: str, timeout: int = 30) -> dict:
    ss = st.session_state
    before = ss.cwd
    wrapped = (
        f"cd {shlex.quote(before)} && {{\n{cmd}\n}}; __rc=$?; "
        f"printf '\\n{MARK}%s\\n' \"$(pwd)\"; exit $__rc"
    )
    t0 = time.time()
    res = dict(cmd=cmd, out="", err="", rc=None, cwd_before=before, cwd_after=before,
               ts=datetime.now().strftime("%H:%M:%S"), ms=0,
               user=ss.conn["user"], host=ss.conn["host"])
    try:
        _, stdout, _ = ss.client.exec_command(wrapped, timeout=timeout)
        chan = stdout.channel
        out_b, err_b = b"", b""
        timed_out = False
        while not chan.exit_status_ready() or chan.recv_ready() or chan.recv_stderr_ready():
            if chan.recv_ready():
                out_b += chan.recv(65536)
            if chan.recv_stderr_ready():
                err_b += chan.recv_stderr(65536)
            if time.time() - t0 > timeout:
                timed_out = True
                chan.close()
                break
            if len(out_b) > MAX_OUT * 2:
                chan.close()
                break
            time.sleep(0.02)
        rc = None if timed_out else chan.recv_exit_status()
        text = out_b.decode("utf-8", "replace")
        if MARK in text:
            body, _, tail = text.rpartition(MARK)
            res["cwd_after"] = tail.strip() or before
            text = body[:-1] if body.endswith("\n") else body
        err = err_b.decode("utf-8", "replace")
        if timed_out:
            err += f"\n[timed out after {timeout}s — interactive/long-running commands aren't supported]"
        if len(text) > MAX_OUT:
            text = text[:MAX_OUT] + "\n… output truncated …"
        res.update(out=text.rstrip(), err=err.rstrip(), rc=rc)
    except Exception as e:  # noqa: BLE001
        res.update(err=f"Connection error: {e}", rc=255)
    res["ms"] = int((time.time() - t0) * 1000)
    if res["cwd_after"] != before:
        ss.cwd = res["cwd_after"]
        ss.path_input = res["cwd_after"]
    return res


def fetch_info() -> str:
    cmd = (
        'echo "## Host";  hostname; uname -srm; '
        'echo; echo "## Uptime"; uptime; '
        'echo; echo "## Disk"; df -h 2>/dev/null | head -8; '
        'echo; echo "## Memory"; free -h 2>/dev/null || vm_stat 2>/dev/null | head -6; '
        'echo; echo "## Logged in"; who 2>/dev/null | head -5'
    )
    r = run_command(cmd)
    return (r["out"] + ("\n" + r["err"] if r["err"] else "")).strip()


def do_connect(host, port, user, auth, password, key_text, passphrase):
    allowed = secret("ALLOWED_HOSTS")
    if allowed:
        hosts = [h.strip() for h in str(allowed).split(",") if h.strip()]
        if host not in hosts:
            raise ValueError("This deployment only allows connections to approved hosts.")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = dict(hostname=host, port=port, username=user, timeout=12, banner_timeout=15,
              auth_timeout=15, look_for_keys=False, allow_agent=False)
    if auth == "Password":
        kw["password"] = password
    else:
        kw["pkey"] = load_pkey(key_text, passphrase)
    client.connect(**kw)
    client.get_transport().set_keepalive(30)
    sftp = client.open_sftp()
    home = sftp.normalize(".")
    st.session_state.update(
        client=client, sftp=sftp, cwd=home, path_input=home, history=[], dl=None,
        conn=dict(host=host, port=port, user=user, since=datetime.now().strftime("%H:%M:%S")),
    )
    st.session_state.info = fetch_info()


def disconnect():
    ss = st.session_state
    for obj in (ss.sftp, ss.client):
        try:
            if obj:
                obj.close()
        except Exception:  # noqa: BLE001
            pass
    ss.update(client=None, sftp=None, conn=None, history=[], info="", dl=None)


def go(path: str):
    ss = st.session_state
    try:
        p = ss.sftp.normalize(path)
        ss.sftp.listdir(p)
        ss.cwd = p
        ss.path_input = p
        ss.dl = None
    except Exception as e:  # noqa: BLE001
        ss.flash = f"Cannot open {path}: {e}"
        ss.path_input = ss.cwd


def go_from_input():
    go(st.session_state.path_input)


def list_dir(path: str):
    sftp = st.session_state.sftp
    entries = []
    for a in sftp.listdir_attr(path):
        mode = a.st_mode or 0
        full = posixpath.join(path, a.filename)
        is_link = stat.S_ISLNK(mode)
        is_dir = stat.S_ISDIR(mode)
        if is_link:
            try:
                is_dir = stat.S_ISDIR(sftp.stat(full).st_mode)
            except Exception:  # noqa: BLE001
                pass
        entries.append(dict(name=a.filename, full=full, is_dir=is_dir, is_link=is_link,
                            size=a.st_size or 0, mtime=a.st_mtime, perms=stat.filemode(mode)))
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].casefold()))
    return entries


# ----------------------------------------------------------------------------- UI pieces
def sidebar():
    with st.sidebar:
        st.markdown("### 🖥️ SSH Console")
        if is_connected():
            c = st.session_state.conn
            st.html(f'<div class="pill"><span class="dot"></span>{esc(c["user"])}@{esc(c["host"])}:{c["port"]}</div>')
            st.caption(f"Connected since {c['since']}")
            if st.button("Disconnect", type="primary"):
                disconnect()
                st.rerun()
            return
        if st.session_state.client is not None:  # dropped
            disconnect()
            st.warning("Connection lost.")
        auth = st.radio("Authentication", ["Password", "Private key"], horizontal=True)
        with st.form("connect"):
            host = st.text_input("Host", placeholder="example.com or 203.0.113.10")
            port = st.number_input("Port", 1, 65535, 22)
            user = st.text_input("Username")
            password, key_text, passphrase = "", "", ""
            if auth == "Password":
                password = st.text_input("Password", type="password")
            else:
                up = st.file_uploader("Private key file", help="OpenSSH/PEM — Ed25519, ECDSA or RSA")
                pasted = st.text_area("…or paste the key", height=100)
                passphrase = st.text_input("Key passphrase (if any)", type="password")
                key_text = pasted.strip() or (up.getvalue().decode("utf-8", "ignore") if up else "")
            go_btn = st.form_submit_button("Connect ⚡", type="primary")
        if go_btn:
            if not host or not user:
                st.error("Host and username are required.")
            else:
                try:
                    with st.spinner("Connecting…"):
                        do_connect(host.strip(), int(port), user.strip(), auth, password, key_text, passphrase)
                    st.rerun()
                except paramiko.AuthenticationException:
                    st.error("Authentication failed.")
                except Exception as e:  # noqa: BLE001
                    st.error(f"Could not connect: {e}")
        with st.expander("🔒 Security notes"):
            st.markdown(
                "- Credentials live only in this browser session's server memory and are never written to disk.\n"
                "- The first-seen host key is trusted automatically (no known_hosts check).\n"
                "- Streamlit Cloud connects from the public internet, so private/VPN-only servers are unreachable.\n"
                "- Use a throw-away or low-privilege account, and keep the app private."
            )


def landing():
    st.html(
        '<div class="hero"><h1>SSH Console</h1>'
        "<p>Connect to any SSH server, run commands and browse files — right from your browser.</p>"
        '<div class="steps">'
        '<div class="step"><b>1 · Connect</b>Enter host, user and a password or private key in the sidebar.</div>'
        '<div class="step"><b>2 · Terminal</b>Run commands with a persistent working directory and quick actions.</div>'
        '<div class="step"><b>3 · Files</b>Browse folders over SFTP, preview text files, download and upload.</div>'
        "</div></div>"
    )


def render_terminal():
    ss = st.session_state
    parts = []
    if not ss.history:
        parts.append('<div class="t-dim">Connected. Type a command below — your working directory is remembered between commands.</div>')
    for h in ss.history:
        parts.append(
            f'<div class="t-line"><span class="t-user">{esc(h["user"])}@{esc(h["host"])}</span>:'
            f'<span class="t-path">{esc(h["cwd_before"])}</span><span class="t-sym">$</span>'
            f'<span class="t-cmd">{esc(h["cmd"])}</span></div>'
        )
        if h["out"]:
            parts.append(f'<pre class="t-out">{esc(h["out"])}</pre>')
        if h["err"]:
            parts.append(f'<pre class="t-err">{esc(h["err"])}</pre>')
        ok = h["rc"] == 0
        parts.append(f'<div class="{"t-ok" if ok else "t-bad"}">exit {h["rc"]} · {h["ms"]} ms · {h["ts"]}</div>')
    st.html(
        '<div class="term"><div class="term-bar"><i style="background:#ff5f56"></i><i style="background:#ffbd2e"></i>'
        f'<i style="background:#27c93f"></i>&nbsp; {esc(ss.conn["user"])}@{esc(ss.conn["host"])} — {esc(ss.cwd)}</div>'
        f'<div class="term-body"><div>{"".join(parts)}</div></div></div>'
    )


def transcript() -> str:
    lines = []
    for h in st.session_state.history:
        lines.append(f'{h["user"]}@{h["host"]}:{h["cwd_before"]}$ {h["cmd"]}')
        if h["out"]:
            lines.append(h["out"])
        if h["err"]:
            lines.append(h["err"])
        lines.append("")
    return "\n".join(lines)


def terminal_tab():
    ss = st.session_state
    cols = st.columns(len(QUICK))
    quick = None
    for col, (label, c) in zip(cols, QUICK):
        if col.button(label, key=f"q_{label}"):
            quick = c
    hist_ph = st.container()
    with st.form("cmd_form", clear_on_submit=True):
        c1, c2 = st.columns([9, 1])
        typed = c1.text_input("command", placeholder="Type a command and press Enter…", label_visibility="collapsed")
        sent = c2.form_submit_button("Run ▶")
    cmd = quick or (typed.strip() if sent else "")
    if cmd:
        if cmd in ("clear", "cls"):
            ss.history = []
        else:
            with st.spinner("Running…"):
                res = run_command(cmd)
            ss.history = (ss.history + [res])[-60:]
    with hist_ph:
        render_terminal()
    b1, b2, _ = st.columns([1, 1, 6])
    if b1.button("🧹 Clear"):
        ss.history = []
        st.rerun()
    b2.download_button("💾 Transcript", transcript(), file_name="ssh-session.txt")
    st.caption("Commands run one at a time (no TTY). Interactive programs such as vim, nano or top won't work — use `top -b -n 1`, `cat`, `less -F` etc.")


def files_tab():
    ss = st.session_state
    if ss.flash:
        st.warning(ss.flash)
        ss.flash = None
    n1, n2, n3, n4 = st.columns([1, 1, 1, 7])
    n1.button("⬆ Up", on_click=go, args=(posixpath.dirname(ss.cwd.rstrip("/")) or "/",))
    n2.button("🏠 Home", on_click=go, args=(".",))
    n3.button("🔄 Refresh", on_click=lambda: None)
    n4.text_input("Path", key="path_input", label_visibility="collapsed", on_change=go_from_input)

    try:
        entries = list_dir(ss.cwd)
    except Exception as e:  # noqa: BLE001
        st.error(f"Cannot list {ss.cwd}: {e}")
        return

    f1, f2 = st.columns([3, 1])
    q = f1.text_input("Filter", placeholder="🔎 Filter by name…", label_visibility="collapsed")
    show_hidden = f2.toggle("Show hidden", value=False)
    view = [e for e in entries if (show_hidden or not e["name"].startswith(".")) and q.lower() in e["name"].lower()]

    n_dirs = sum(e["is_dir"] for e in view)
    st.caption(f"{n_dirs} folders · {len(view) - n_dirs} files in `{ss.cwd}`")
    if not view:
        st.info("This folder is empty (or nothing matches your filter).")
    else:
        df = pd.DataFrame([{
            "": "📁" if e["is_dir"] else ("🔗" if e["is_link"] else "📄"),
            "Name": e["name"],
            "Size": "—" if e["is_dir"] else human(e["size"]),
            "Modified": datetime.fromtimestamp(e["mtime"]).strftime("%Y-%m-%d %H:%M") if e["mtime"] else "",
            "Permissions": e["perms"],
        } for e in view])
        ev = st.dataframe(df, hide_index=True, on_select="rerun", selection_mode="single-row",
                          key=f"tbl::{ss.cwd}::{q}::{show_hidden}", height=min(420, 40 + 35 * len(view)))
        rows = ev.selection.rows if ev and ev.selection else []
        if rows:
            e = view[rows[0]]
            st.markdown(f"**Selected:** `{e['full']}`")
            if e["is_dir"]:
                st.button("📂 Open folder", on_click=go, args=(e["full"],))
            else:
                preview_file(e)

    st.divider()
    ups = st.file_uploader("Upload files to this folder", accept_multiple_files=True, key=f"up{ss.up_n}")
    if ups and st.button("⬆ Upload"):
        try:
            for f in ups:
                ss.sftp.putfo(f, posixpath.join(ss.cwd, f.name))
            ss.up_n += 1
            st.toast(f"Uploaded {len(ups)} file(s)", icon="✅")
            st.rerun()
        except Exception as ex:  # noqa: BLE001
            st.error(f"Upload failed: {ex}")


def preview_file(e):
    ss = st.session_state
    c1, c2 = st.columns([1, 1])
    if e["size"] <= MAX_DOWNLOAD:
        if c1.button("📦 Prepare download"):
            try:
                with ss.sftp.open(e["full"], "rb") as fh:
                    ss.dl = (e["full"], fh.read())
            except Exception as ex:  # noqa: BLE001
                st.error(f"Read failed: {ex}")
        if ss.dl and ss.dl[0] == e["full"]:
            c2.download_button(f"⬇ Download ({human(len(ss.dl[1]))})", ss.dl[1], file_name=e["name"])
    else:
        c1.info(f"Too large to download here ({human(e['size'])}; limit {human(MAX_DOWNLOAD)}).")
    try:
        with ss.sftp.open(e["full"], "rb") as fh:
            data = fh.read(MAX_PREVIEW)
    except Exception as ex:  # noqa: BLE001
        st.error(f"Cannot read file: {ex}")
        return
    if b"\x00" in data[:4096]:
        st.caption("Binary file — preview not available.")
        return
    ext = e["name"].rsplit(".", 1)[-1].lower() if "." in e["name"] else ""
    st.code(data.decode("utf-8", "replace"), language=LANGS.get(ext), line_numbers=True)
    if e["size"] > MAX_PREVIEW:
        st.caption(f"Showing first {human(MAX_PREVIEW)} of {human(e['size'])}.")


def server_tab():
    c1, _ = st.columns([1, 6])
    if c1.button("🔄 Refresh info"):
        st.session_state.info = fetch_info()
    st.code(st.session_state.info or "No data yet.", language=None)


# ----------------------------------------------------------------------------- main
init_state()
st.markdown(CSS, unsafe_allow_html=True)
gate()
sidebar()

if not is_connected():
    landing()
    st.stop()

tab_term, tab_files, tab_info = st.tabs(["⌨️ Terminal", "📂 Files", "📊 Server"])
with tab_term:      # must run before the Files tab so `cd` can sync the path box
    terminal_tab()
with tab_files:
    files_tab()
with tab_info:
    server_tab()
