#!/usr/bin/env python
"""blogsite 三边同步：本地 ↔ GitHub ↔ 飞书。

目录约定（改代码前先读）：

    blogsite/                      ← 本仓库根 = https://starnever0.github.io/blog/
    ├── index.html                 自动生成的首页索引（别手改，会被覆盖）
    ├── 2026-10-30-xxx.html        单页，平铺在根目录，一个文件一页
    ├── agent-basics/index.html    栏目页，只在 --build-collections 时生成
    ├── collections/manifest.yaml  分类元数据，不发布
    ├── .state/                    基线与临时镜像区，不发布
    └── sync_blog.py               本脚本

为什么单页放仓库根而不是 blog/ 子目录：GitHub 项目页的站点根本来就是
/blog/，仓库里再套一层 blog/ 就会变成 /blog/blog/。多一层路径没有意义。

三条设计约束：

1. **只增不删。** 链路上没有任何删除动作。本地删了文件，远端保留，
   日报里标一句「远端多出来」，由人决定要不要清。
2. **不建栏目。** 栏目页只在显式 --build-collections 时生成，依据是
   manifest 里已经写好的 collection 字段。脚本不做任何分类判断。
3. **失败就停，不重试。** 重试只会制造半同步状态，把错误原样报出来更有价值。

用法：
    python sync_blog.py                       # 完整同步 + 日报
    python sync_blog.py --check               # 只对账，不写不推
    python sync_blog.py --no-feishu           # 只推 GitHub
    python sync_blog.py --build-collections   # 按 manifest 生成栏目页
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("缺 pyyaml：需要 pip install pyyaml")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "collections" / "manifest.yaml"
STATE_DIR = ROOT / ".state"
STATE_FILE = STATE_DIR / "index.json"
CONFIG_FILE = STATE_DIR / "config.json"
STAGE = STATE_DIR / "feishu_stage"

INDEX_NAME = "index.html"
SKIP_NAMES = {INDEX_NAME, "sync_blog.py"}

DEFAULT_CONFIG = {
    "base_url": "https://starnever0.github.io/blog",
    "repo": "https://github.com/Starnever0/blog",
    "feishu_folder_token": "",
    "feishu_wiki_space_id": "",
    "git_remote": "origin",
    "git_branch": "main",
}


# ---------------------------------------------------------------- 基础工具

def run(cmd: list[str], cwd: Path = ROOT) -> subprocess.CompletedProcess:
    """统一跑子进程。Windows 上 .cmd 必须过 cmd /c，否则 CreateProcess 拒绝。"""
    if cmd and str(cmd[0]).lower().endswith((".cmd", ".bat")):
        cmd = ["cmd", "/c", *cmd]
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def lark_bin() -> str | None:
    return shutil.which("lark-cli") or shutil.which("lark-cli.cmd")


def load_config() -> dict:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    merged = {**DEFAULT_CONFIG, **cfg}
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged


def sha_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def title_of(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return path.stem
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    if m:
        t = re.sub(r"\s+", " ", m.group(1)).strip()
        if t:
            return t
    return path.stem


def date_of(path: Path) -> str:
    m = re.match(r"(\d{4}-\d{2}-\d{2})", path.name)
    if m:
        return m.group(1)
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 页面清单

def scan_pages() -> dict[str, Path]:
    """根目录下的 .html = 单页。子目录里的是栏目页，不算单页。"""
    pages: dict[str, Path] = {}
    for p in sorted(ROOT.iterdir()):
        if p.is_file() and p.suffix.lower() == ".html" and p.name not in SKIP_NAMES:
            pages[p.name] = p
    return pages


def load_manifest() -> dict:
    if not MANIFEST.exists():
        return {"site": {}, "collections": {}, "pages": []}
    try:
        data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        sys.exit(f"manifest.yaml 解析失败：{exc}\n先手工修好，脚本不动它。")
    data.setdefault("site", {})
    data.setdefault("collections", None)
    data.setdefault("pages", None)
    if data["pages"] is None:
        data["pages"] = []
    if data["collections"] is None:
        data["collections"] = {}
    return data


def save_manifest(data: dict) -> None:
    """只重写 site 之后的结构化部分，保留文件开头的说明注释。"""
    head = ""
    if MANIFEST.exists():
        raw = MANIFEST.read_text(encoding="utf-8")
        idx = raw.find("\nsite:")
        head = raw[: idx + 1] if idx >= 0 else ""
    body = yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(head + body, encoding="utf-8")


def reconcile_entries(manifest: dict, pages: dict[str, Path]) -> tuple[list[str], list[str], list[str]]:
    """让 manifest 的 pages 和磁盘上的实际文件严格对齐。

    - 新增：磁盘上有、manifest 没有的，补一条，collection 默认 null
    - 摘除：manifest 有、磁盘上已不存在的，删掉这条。否则首页会挂一个 404 死链
    - 刷新：title / date 按文件重新算（这两个字段是派生值，不是人的判断）

    **唯一不动的是 collection**——分类是人看过之后下的判断，脚本绝不覆盖。
    """
    live = {p.relative_to(ROOT).as_posix(): p for p in pages.values()}
    known = {e.get("file") for e in manifest["pages"]}

    added = [rel for rel in live if rel not in known]
    for rel in added:
        path = live[rel]
        manifest["pages"].append({
            "slug": path.stem,
            "title": title_of(path),
            "date": date_of(path),
            "file": rel,
            "collection": None,
        })

    pruned = [e["file"] for e in manifest["pages"] if e.get("file") and e["file"] not in live]
    manifest["pages"] = [e for e in manifest["pages"] if e.get("file") in live]

    refreshed = []
    for entry in manifest["pages"]:
        path = live[entry["file"]]
        new_title, new_date = title_of(path), date_of(path)
        if entry.get("title") != new_title or entry.get("date") != new_date:
            entry["title"], entry["date"] = new_title, new_date
            refreshed.append(entry["file"])

    manifest["pages"].sort(key=lambda e: (e.get("date") or "", e.get("file") or ""))
    return added, pruned, refreshed


# ---------------------------------------------------------------- 生成页面

SHELL_CSS = """  :root { --ink:#1a1a1a; --muted:#6b6b6b; --line:#e6e4e0; --accent:#c0392b; --bg:#fdfdfc; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:16px/1.7 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }
  main { max-width:700px; margin:0 auto; padding:64px 24px 96px; }
  .kicker { color:var(--accent); font-size:12px; letter-spacing:.12em; text-transform:uppercase; }
  h1 { font-size:30px; margin:8px 0 12px; letter-spacing:-.01em; }
  .desc { color:var(--muted); margin-bottom:44px; }
  ul { list-style:none; padding:0; margin:0; }
  li { border-top:1px solid var(--line); padding:15px 0; display:flex;
       justify-content:space-between; align-items:baseline; gap:16px; }
  li a { color:var(--ink); text-decoration:none; font-weight:600; }
  li a:hover { color:var(--accent); }
  .d { color:var(--muted); font-size:13px; font-family:ui-monospace,Consolas,monospace;
       white-space:nowrap; }
  .empty { color:var(--muted); }
  .sec { margin-top:52px; }
  footer { margin-top:64px; font-size:13px; color:var(--muted); }"""


def _page(title: str, kicker: str, h1: str, desc: str, body: str, footer: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
{SHELL_CSS}
</style>
</head>
<body>
<main>
  <div class="kicker">{kicker}</div>
  <h1>{h1}</h1>
  <div class="desc">{desc}</div>
{body}
{footer}
</main>
</body>
</html>
"""


def build_index(manifest: dict) -> str:
    """生成首页：全部单页按日期倒序 + 已组装栏目的入口。"""
    pages = sorted(manifest["pages"], key=lambda e: (e.get("date") or "", e.get("file") or ""), reverse=True)
    items = "\n".join(
        f'    <li><a href="{Path(e["file"]).name}">{e.get("title") or e["slug"]}</a>'
        f'<span class="d">{e.get("date") or ""}</span></li>'
        for e in pages
    )
    list_html = f"<ul>\n{items}\n  </ul>" if items else '<p class="empty">还没有页面。</p>'

    collections = manifest.get("collections") or {}
    if collections:
        links = "\n".join(
            f'    <li><a href="{slug}/">{meta.get("title") or slug}</a>'
            f'<span class="d">{len([e for e in manifest["pages"] if e.get("collection") == slug])} 篇</span></li>'
            for slug, meta in collections.items()
        )
        sec = f'\n  <div class="sec">\n  <div class="kicker">专题</div>\n  <ul>\n{links}\n  </ul>\n  </div>'
    else:
        sec = ""

    return _page(
        title="Starnever 单页",
        kicker="全部页面",
        h1="Starnever",
        desc="多模态算法 · Agent 开发 · 大模型应用",
        body=list_html + sec,
        footer='<footer>按日期倒序 · 共 %d 篇</footer>' % len(pages),
    )


def build_collections(manifest: dict) -> list[str]:
    """按 manifest 的 collection 字段生成栏目页。没声明 collection 的不出现。"""
    built = []
    collections = manifest.get("collections") or {}
    for slug, meta in collections.items():
        meta = meta or {}
        members = [e for e in manifest["pages"] if e.get("collection") == slug]
        if members:
            items = "\n".join(
                f'    <li><a href="../{Path(e["file"]).name}">{e.get("title") or e["slug"]}</a>'
                f'<span class="d">{e.get("date") or ""}</span></li>'
                for e in members
            )
            body = f"<ul>\n{items}\n  </ul>"
        else:
            body = '<p class="empty">这个栏目还没有收录页面。</p>'
        html = _page(
            title=meta.get("title") or slug,
            kicker="专题",
            h1=meta.get("title") or slug,
            desc=meta.get("description") or "",
            body=body,
            footer='<footer><a href="../">← 全部页面</a></footer>',
        )
        out = ROOT / slug / INDEX_NAME
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        built.append(f"{slug}/{INDEX_NAME}")
    return built


# ---------------------------------------------------------------- GitHub

def git_sync(cfg: dict) -> dict:
    run(["git", "add", "-A"])
    status = run(["git", "status", "--porcelain"]).stdout.strip()
    if not status:
        return {"committed": False, "pushed": False, "noop": True, "detail": "工作区无变化"}
    n = len([l for l in status.splitlines() if l.strip()])
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    commit = run(["git", "commit", "-m", f"sync: {n} 个文件 · {stamp}"])
    if commit.returncode != 0:
        return {"committed": False, "pushed": False,
                "detail": (commit.stderr or commit.stdout).strip()[:400]}
    push = None
    for attempt, wait in enumerate((0, 5, 15), start=1):
        if wait:
            time.sleep(wait)
        push = run(["git", "push", cfg["git_remote"], cfg["git_branch"]])
        if push.returncode == 0:
            break
        err = (push.stderr or push.stdout).strip()
        # 只有网络/ TLS 类错误值得重试；鉴权失败、non-fast-forward 重试也没用
        if not any(k in err.lower() for k in ("handshake", "could not resolve",
                                              "connection", "timed out", "timeout",
                                              "unable to access")):
            break
    if push.returncode != 0:
        return {"committed": True, "pushed": False,
                "detail": f"push 失败（重试 {attempt} 次）：{(push.stderr or push.stdout).strip()[:400]}"}
    return {"committed": True, "pushed": True, "detail": f"提交并推送 {n} 个文件"}


# ---------------------------------------------------------------- 飞书

def stage_pages(pages: dict[str, Path], manifest: dict) -> Path:
    """把要镜像的文件挑进 .state/feishu_stage，保留 mtime 让 smart 去重起作用。

    不直接 --local-dir . 的原因：那会把 .git / collections / .state 一起推上去。
    """
    if STAGE.parent != STATE_DIR:
        raise RuntimeError(f"staging 目录路径异常，拒绝清理：{STAGE}")
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)

    for name, path in pages.items():
        shutil.copy2(path, STAGE / name)

    index = ROOT / INDEX_NAME
    if index.exists():
        shutil.copy2(index, STAGE / INDEX_NAME)

    for slug in (manifest.get("collections") or {}):
        src = ROOT / slug / INDEX_NAME
        if src.exists():
            (STAGE / slug).mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, STAGE / slug / INDEX_NAME)
    return STAGE


def feishu_push(cfg: dict, manifest: dict, pages: dict[str, Path]) -> dict:
    token = cfg.get("feishu_folder_token") or ""
    if not token:
        return {"ok": False, "detail": "config.json 里没填 feishu_folder_token"}
    exe = lark_bin()
    if not exe:
        return {"ok": False, "detail": "找不到 lark-cli"}

    stage = stage_pages(pages, manifest)
    proc = run([exe, "drive", "+push",
                "--local-dir", str(stage),
                "--folder-token", token,
                "--as", "user",
                "--if-exists", "smart",
                "--format", "json"])
    raw = (proc.stdout or "").strip()
    start = raw.find("{")
    if start < 0:
        return {"ok": False, "detail": (proc.stderr or raw or "无输出")[:400]}
    try:
        data = json.loads(raw[start:])
    except Exception:
        return {"ok": False, "detail": raw[:400]}
    if not data.get("ok"):
        return {"ok": False, "detail": json.dumps(data.get("error") or data, ensure_ascii=False)[:400]}
    return {"ok": True, "summary": (data.get("data") or {}).get("summary") or {}}


def feishu_listing(cfg: dict) -> dict[str, dict]:
    """递归列出飞书镜像目录，返回「相对路径 → {token, url}」。"""
    token = cfg.get("feishu_folder_token") or ""
    exe = lark_bin()
    if not token or not exe:
        return {}
    out: dict[str, dict] = {}

    def walk(folder_token: str, prefix: str) -> None:
        proc = run([exe, "drive", "files", "list",
                    "--folder-token", folder_token,
                    "--as", "user", "--format", "json", "--page-all"])
        raw = (proc.stdout or "").strip()
        start = raw.find("{")
        if start < 0:
            raise RuntimeError((proc.stderr or raw or "list 无输出")[:300])
        data = json.loads(raw[start:])
        if not data.get("ok"):
            raise RuntimeError(json.dumps(data.get("error") or data, ensure_ascii=False)[:300])
        for f in (data.get("data") or {}).get("files") or []:
            name = f.get("name") or ""
            if f.get("type") == "folder":
                walk(f.get("token") or "", f"{prefix}{name}/")
            elif f.get("type") == "file":
                out[f"{prefix}{name}"] = {"token": f.get("token") or "", "url": f.get("url") or ""}

    try:
        walk(token, "")
    except Exception as exc:
        print(f"  ! 列飞书目录失败：{exc}", file=sys.stderr)
    return out


def feishu_index(cfg: dict) -> dict[str, str]:
    """只要 URL 的薄封装（日报和栏目页用）。"""
    return {k: v.get("url") or "" for k, v in feishu_listing(cfg).items()}


# ---------------------------------------------------------------- 飞书知识库

def wiki_node_title(entry: dict, name: str) -> str:
    """知识库里的节点标题：日期前缀 + 文章标题。

    加日期是因为飞书知识库的树按标题排序，不加日期就变成字典序，
    时间线彻底散掉。日期来自文件名，是派生值，不额外维护。
    """
    date = entry.get("date") or ""
    title = entry.get("title") or Path(name).stem
    return f"{date} · {title}" if date else title


def wiki_existing_nodes(exe: str, space_id: str) -> dict[str, dict]:
    """读飞书知识库现有节点，按 obj_token（= 云空间文件 token）索引。

    去重依据是飞书那边的真实状态，不是本地状态文件。状态文件丢了、
    换机器了，都不会重复建节点。
    """
    proc = run([exe, "wiki", "+node-list", "--space-id", space_id,
                "--as", "user", "--format", "json", "--page-all"])
    raw = (proc.stdout or "").strip()
    start = raw.find("{")
    if start < 0:
        raise RuntimeError((proc.stderr or raw or "node-list 无输出")[:300])
    data = json.loads(raw[start:])
    if not data.get("ok"):
        raise RuntimeError(json.dumps(data.get("error") or data, ensure_ascii=False)[:300])
    nodes = (data.get("data") or {}).get("nodes") or []
    return {n.get("obj_token"): n for n in nodes if n.get("obj_token")}


def feishu_wiki_sync(cfg: dict, pages: dict[str, Path], manifest: dict,
                     remote: dict[str, dict], state: dict) -> dict:
    """给每篇还没挂过的文章在知识库里补一个快捷方式节点。

    只增不删、不分类、不建子层：所有节点平铺在知识库根，排序靠飞书里的手工拖动
    或创建顺序。节点是 shortcut，指向云空间里那份 HTML，所以点开是飞书自己的
    预览，不是下载。
    """
    space_id = (cfg.get("feishu_wiki_space_id") or "").strip()
    if not space_id:
        return {"ok": True, "disabled": True, "detail": "未配置 feishu_wiki_space_id，跳过"}
    exe = lark_bin()
    if not exe:
        return {"ok": False, "detail": "找不到 lark-cli"}

    try:
        existing = wiki_existing_nodes(exe, space_id)
    except Exception as exc:
        return {"ok": False, "detail": f"读知识库节点失败：{exc}"}

    by_file = {e.get("file"): e for e in manifest["pages"]}
    nodes = state.setdefault("wiki_nodes", {})
    created, skipped, failed = [], [], []

    for name in sorted(pages):
        token = (remote.get(name) or {}).get("token")
        entry = by_file.get(name) or {}
        title = wiki_node_title(entry, name)

        # 知识库那边已经有指向这个文件的节点：登记进状态就够，不重复建
        if token and token in existing:
            hit = existing[token]
            nodes[name] = {
                "node_token": hit.get("node_token") or "",
                "obj_token": token,
                "title": hit.get("title") or title,
                "url": hit.get("url") or f"https://my.feishu.cn/wiki/{hit.get('node_token') or ''}",
            }
            skipped.append(name)
            continue

        if not token:
            failed.append(f"{name}（飞书里没找到这个文件，先确保镜像成功）")
            continue

        proc = run([exe, "wiki", "+node-create",
                    "--space-id", space_id,
                    "--node-type", "shortcut",
                    "--obj-type", "file",
                    "--origin-node-token", token,
                    "--title", title,
                    "--as", "user", "--format", "json"])
        raw = (proc.stdout or "").strip()
        start = raw.find("{")
        if start < 0:
            failed.append(f"{name}（{(proc.stderr or raw or '无输出')[:120]}）")
            continue
        try:
            data = json.loads(raw[start:])
        except Exception:
            failed.append(f"{name}（返回无法解析）")
            continue
        if not data.get("ok"):
            failed.append(f"{name}（{json.dumps(data.get('error') or data, ensure_ascii=False)[:160]}）")
            continue
        # 返回结构有两种形态：data 直接是节点，或 data.node 包一层，都得吃得下
        payload = data.get("data") or {}
        node = payload.get("node") if isinstance(payload.get("node"), dict) else payload
        nodes[name] = {
            "node_token": node.get("node_token") or "",
            "obj_token": node.get("obj_token") or token,
            "title": title,
            "url": node.get("url") or f"https://my.feishu.cn/wiki/{node.get('node_token') or ''}",
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        created.append(name)

    if created or skipped:
        save_state(state)
    return {"ok": not failed, "created": created, "skipped": skipped, "failed": failed}


# ---------------------------------------------------------------- 对账

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"pages": {}}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def diff_state(pages: dict[str, Path], state: dict) -> tuple[list[str], list[str]]:
    new, changed = [], []
    seen = state.get("pages") or {}
    for name, path in pages.items():
        sha = sha_of(path)
        old = seen.get(name)
        if not old:
            new.append(name)
        elif old.get("sha") != sha:
            changed.append(name)
    return new, changed


def main() -> int:
    ap = argparse.ArgumentParser(description="blogsite 三边同步")
    ap.add_argument("--check", action="store_true", help="只对账，不写入不推送")
    ap.add_argument("--no-feishu", action="store_true", help="跳过飞书镜像")
    ap.add_argument("--no-git", action="store_true", help="跳过 git 提交与推送")
    ap.add_argument("--no-wiki", action="store_true", help="跳过飞书知识库节点同步")
    ap.add_argument("--build-collections", action="store_true", help="按 manifest 生成栏目页")
    args = ap.parse_args()

    cfg = load_config()
    base = cfg["base_url"].rstrip("/")
    repo = cfg["repo"].rstrip("/")
    branch = cfg["git_branch"]

    pages = scan_pages()
    manifest = load_manifest()
    state = load_state()

    print(f"blogsite 同步 · {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"本地  {ROOT}")
    print(f"仓库  {repo}")
    print(f"单页  {len(pages)} 个\n")

    added, pruned, refreshed = [], [], []
    if not args.check:
        added, pruned, refreshed = reconcile_entries(manifest, pages)
        if added or pruned or refreshed:
            save_manifest(manifest)
    if added:
        print(f"新入册 {len(added)} 条：{', '.join(added)}")
    if pruned:
        print(f"摘除 {len(pruned)} 条（文件已不存在，避免首页死链）：{', '.join(pruned)}")
    if refreshed:
        print(f"刷新 {len(refreshed)} 条标题/日期：{', '.join(refreshed)}")
    if added or pruned or refreshed:
        print()

    built: list[str] = []
    if args.build_collections and not args.check:
        built = build_collections(manifest)
        if built:
            print("生成栏目页：" + "、".join(built) + "\n")

    # 首页每次都重生成，成本可忽略，保证索引不落后
    if not args.check:
        (ROOT / INDEX_NAME).write_text(build_index(manifest), encoding="utf-8")

    # 变化检测必须在推送前算，推完基线就变了
    new, changed = diff_state(pages, state)

    git_info = {"committed": False, "pushed": False, "detail": "跳过"}
    if not args.check and not args.no_git:
        git_info = git_sync(cfg)

    feishu_info = {"ok": True, "detail": "跳过"}
    if not args.check and not args.no_feishu:
        feishu_info = feishu_push(cfg, manifest, pages)

    listing = feishu_listing(cfg)
    remote = {k: v.get("url") or "" for k, v in listing.items()}

    wiki_info = {"ok": True, "detail": "跳过"}
    if not args.check and not args.no_feishu and not args.no_wiki:
        wiki_info = feishu_wiki_sync(cfg, pages, manifest, listing, state)

    local_names = set(pages.keys())
    missing_remote = sorted(local_names - set(remote.keys()))
    extra_remote = sorted({n for n in remote if "/" not in n} - local_names - {INDEX_NAME})

    print("-" * 64)
    print(f"新增 {len(new)} / 修改 {len(changed)} / "
          f"飞书缺 {len(missing_remote)} / 飞书多 {len(extra_remote)}")
    print("-" * 64)

    def show(name: str, tag: str) -> None:
        path = pages[name]
        rel = path.relative_to(ROOT).as_posix()
        entry = next((e for e in manifest["pages"] if e.get("file") == rel), {})
        print(f"\n[{tag}] {entry.get('title') or name}")
        print(f"  本地   {path}")
        print(f"  站点   {base}/{rel}")
        print(f"  仓库   {repo}/blob/{branch}/{rel}")
        print(f"  飞书   {remote.get(name) or '（未在远端找到）'}")
        node = (state.get("wiki_nodes") or {}).get(name) or {}
        if node.get("url"):
            print(f"  知识库 {node['url']}")

    for n in new:
        show(n, "新增")
    for n in changed:
        show(n, "修改")
    if not new and not changed:
        for n in sorted(local_names):
            show(n, "未变")

    if missing_remote:
        print("\n飞书缺（本地有、远端没有）：")
        for n in missing_remote:
            print(f"  - {n}")
    if extra_remote:
        print("\n飞书多（远端有、本地没有；脚本不删远端）：")
        for n in extra_remote:
            print(f"  - {n}")

    print("\n" + "-" * 64)
    print(f"首页   {base}/")
    print(f"GitHub {git_info['detail']}")
    if feishu_info.get("ok"):
        s = feishu_info.get("summary") or {}
        print(f"飞书   上传 {s.get('uploaded', 0)} / 跳过 {s.get('skipped', 0)}"
              f" / 失败 {s.get('failed', 0)}")
    else:
        print(f"飞书   失败 —— {feishu_info.get('detail')}")

    if wiki_info.get("disabled") or wiki_info.get("detail") == "跳过":
        pass
    elif wiki_info.get("ok") and not wiki_info.get("created"):
        print(f"知识库  无新增节点（已有 {len(wiki_info.get('skipped') or [])} 条）")
    else:
        c = wiki_info.get("created") or []
        print(f"知识库  新增 {len(c)} 个节点" + (f"：{', '.join(c)}" if c else ""))
        for f in wiki_info.get("failed") or []:
            print(f"        ! {f}")

    git_ok = bool(git_info.get("pushed") or git_info.get("noop") or args.no_git)
    if not args.check and git_ok:
        for name, path in pages.items():
            state.setdefault("pages", {})[name] = {
                "sha": sha_of(path),
                "title": title_of(path),
                "synced_at": datetime.now().isoformat(timespec="seconds"),
            }
        save_state(state)
        print("基线已更新。")
    elif not args.check:
        print("GitHub 未推送成功，基线保持不动——下次会重新报这批文件。")

    ok = args.check or git_ok
    return 0 if ok and feishu_info.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
