#!/usr/bin/env python
"""blogsite 三边同步：本地 ↔ GitHub ↔ 飞书。

设计约束（改代码前先读这段）：

1. **只增不删。** 这条链路上没有任何删除动作。本地删了文件，远端保留，
   脚本只在日报里标一句「远端多出来」，由人决定要不要清。
2. **不建栏目。** 栏目的生成只在显式 --build-collections 时发生，
   而且依据是 manifest 里已经写好的 collection 字段。脚本自己不做分类判断。
3. **失败就停，不重试。** 网络或鉴权出问题时重试只会制造半同步状态，
   把错误原样报出来比"再试一次"有价值。

用法：
    python sync_blog.py                  # 完整同步 + 日报
    python sync_blog.py --check          # 只对账，不上传不推送
    python sync_blog.py --no-feishu      # 只推 GitHub
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
BLOG = ROOT / "blog"
STATE_DIR = ROOT / ".state"
MANIFEST = ROOT / "collections" / "manifest.yaml"
STATE_FILE = STATE_DIR / "index.json"
CONFIG_FILE = STATE_DIR / "config.json"

DEFAULT_CONFIG = {
    "base_url": "https://starnever0.github.io/blog",
    "feishu_folder_token": "",
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
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    else:
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
    return m.group(1) if m else ""


# ---------------------------------------------------------------- 页面清单

def scan_pages() -> dict[str, Path]:
    """blog/ 下第一层的 .html = 单页。子目录里的 index.html 是栏目页，不算单页。"""
    pages: dict[str, Path] = {}
    if not BLOG.exists():
        return pages
    for p in sorted(BLOG.iterdir()):
        if p.is_file() and p.suffix.lower() == ".html":
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
    data.setdefault("pages", [])
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


def ensure_entries(manifest: dict, pages: dict[str, Path]) -> list[str]:
    """把没入册的页面补进去。已存在的条目一个字段都不动——collection 是你的手写判断。"""
    known = {e.get("file") for e in manifest["pages"]}
    added = []
    for name, path in pages.items():
        rel = path.relative_to(ROOT).as_posix()
        if rel in known:
            continue
        manifest["pages"].append({
            "slug": path.stem,
            "title": title_of(path),
            "date": date_of(path),
            "file": rel,
            "collection": None,
        })
        added.append(rel)
    if added:
        manifest["pages"].sort(key=lambda e: (e.get("date") or "", e.get("file") or ""))
    return added


# ---------------------------------------------------------------- 栏目页

COLLECTION_TPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ --ink:#1a1a1a; --muted:#6b6b6b; --line:#e5e5e5; --accent:#c0392b; --bg:#fdfdfc; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
         font:16px/1.7 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }}
  main {{ max-width:680px; margin:0 auto; padding:64px 24px 96px; }}
  .kicker {{ color:var(--accent); font-size:12px; letter-spacing:.12em; text-transform:uppercase; }}
  h1 {{ font-size:30px; margin:8px 0 10px; letter-spacing:-.01em; }}
  .desc {{ color:var(--muted); margin-bottom:44px; }}
  ul {{ list-style:none; padding:0; }}
  li {{ border-top:1px solid var(--line); padding:16px 0; }}
  li a {{ color:var(--ink); text-decoration:none; font-weight:600; }}
  li a:hover {{ color:var(--accent); }}
  .d {{ color:var(--muted); font-size:13px; font-family:ui-monospace,Consolas,monospace; }}
  .empty {{ color:var(--muted); }}
  footer {{ margin-top:56px; font-size:13px; color:var(--muted); }}
  footer a {{ color:var(--muted); }}
</style>
</head>
<body>
<main>
  <div class="kicker">专题</div>
  <h1>{title}</h1>
  <div class="desc">{desc}</div>
  {list}
  <footer><a href="../">← 全部页面</a></footer>
</main>
</body>
</html>
"""


def build_collections(manifest: dict) -> list[str]:
    """按 manifest 的 collection 字段生成栏目页。没声明 collection 的页面不会出现在这里。"""
    built = []
    collections = manifest.get("collections") or {}
    for slug, meta in collections.items():
        meta = meta or {}
        members = [e for e in manifest["pages"] if e.get("collection") == slug]
        if members:
            items = "\n".join(
                f'    <li><a href="../{Path(e["file"]).name}">{e.get("title") or e["slug"]}</a>'
                f'<div class="d">{e.get("date") or ""}</div></li>'
                for e in members
            )
            body_list = f"<ul>\n{items}\n  </ul>"
        else:
            body_list = '<p class="empty">这个栏目还没有收录页面。</p>'
        html = COLLECTION_TPL.format(
            title=meta.get("title") or slug,
            desc=meta.get("description") or "",
            list=body_list,
        )
        out = BLOG / slug / "index.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        built.append(f"blog/{slug}/index.html")
    return built


# ---------------------------------------------------------------- GitHub

def git_sync(cfg: dict, do_push: bool) -> dict:
    run(["git", "add", "-A"])
    status = run(["git", "status", "--porcelain"]).stdout.strip()
    if not status:
        return {"committed": False, "pushed": False, "detail": "工作区无变化"}
    n = len([l for l in status.splitlines() if l.strip()])
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    commit = run(["git", "commit", "-m", f"sync: {n} 个文件 · {stamp}"])
    if commit.returncode != 0:
        return {"committed": False, "pushed": False,
                "detail": (commit.stderr or commit.stdout).strip()[:400]}
    if not do_push:
        return {"committed": True, "pushed": False, "detail": f"已提交 {n} 个文件（未推送）"}
    push = run(["git", "push", cfg["git_remote"], cfg["git_branch"]])
    if push.returncode != 0:
        return {"committed": True, "pushed": False,
                "detail": f"push 失败：{(push.stderr or push.stdout).strip()[:400]}"}
    return {"committed": True, "pushed": True, "detail": f"提交并推送 {n} 个文件"}


# ---------------------------------------------------------------- 飞书

def feishu_push(cfg: dict) -> dict:
    token = cfg.get("feishu_folder_token") or ""
    if not token:
        return {"ok": False, "detail": "config.json 里没填 feishu_folder_token"}
    exe = lark_bin()
    if not exe:
        return {"ok": False, "detail": "找不到 lark-cli"}

    proc = run([exe, "drive", "+push",
                "--local-dir", "blog",
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
    summary = (data.get("data") or {}).get("summary") or {}
    return {"ok": True, "summary": summary}


def feishu_index(cfg: dict) -> dict[str, str]:
    """递归列出飞书镜像目录，返回「相对 blog/ 的路径 → 飞书 URL」。"""
    token = cfg.get("feishu_folder_token") or ""
    exe = lark_bin()
    if not token or not exe:
        return {}

    out: dict[str, str] = {}

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
                out[f"{prefix}{name}"] = f.get("url") or ""

    try:
        walk(token, "")
    except Exception as exc:
        print(f"  ! 列飞书目录失败：{exc}", file=sys.stderr)
    return out


# ---------------------------------------------------------------- 对账与日报

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
    ap.add_argument("--build-collections", action="store_true", help="按 manifest 生成栏目页")
    args = ap.parse_args()

    cfg = load_config()
    base = cfg["base_url"].rstrip("/")

    pages = scan_pages()
    manifest = load_manifest()
    state = load_state()

    print(f"blogsite 同步 · {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"本地 {ROOT}")
    print(f"共 {len(pages)} 个单页\n")

    # 1) 入册
    added = []
    if not args.check:
        added = ensure_entries(manifest, pages)
        if added:
            save_manifest(manifest)
    if added:
        print(f"新入册 {len(added)} 条：{', '.join(added)}\n")

    # 2) 栏目页
    built: list[str] = []
    if args.build_collections and not args.check:
        built = build_collections(manifest)
        if built:
            print("生成栏目页：" + "、".join(built) + "\n")

    # 3) 变化检测（必须在推送前算，推完基线就变了）
    new, changed = diff_state(pages, state)

    # 4) GitHub
    git_info = {"committed": False, "pushed": False, "detail": "跳过"}
    if not args.check and not args.no_git:
        git_info = git_sync(cfg, do_push=True)

    # 5) 飞书
    feishu_info = {"ok": True, "detail": "跳过"}
    if not args.check and not args.no_feishu:
        feishu_info = feishu_push(cfg)

    # 6) 对账
    remote = feishu_index(cfg)
    local_names = set(pages.keys())
    missing_remote = sorted(local_names - set(remote.keys()))
    extra_remote = sorted({n for n in remote if "/" not in n} - local_names)

    # 7) 日报
    print("-" * 62)
    print(f"新增 {len(new)} / 修改 {len(changed)} / "
          f"飞书缺 {len(missing_remote)} / 飞书多 {len(extra_remote)}")
    print("-" * 62)

    def show(name: str, tag: str) -> None:
        path = pages[name]
        entry = next((e for e in manifest["pages"] if e.get("file") == path.relative_to(ROOT).as_posix()), {})
        print(f"\n[{tag}] {entry.get('title') or name}")
        print(f"  本地   {path}")
        print(f"  GitHub {base}/{name}")
        url = remote.get(name)
        print(f"  飞书   {url or '（本次未列出，见上方飞书同步结果）'}")

    for n in new:
        show(n, "新增")
    for n in changed:
        show(n, "修改")
    if not new and not changed:
        rest = [n for n in sorted(local_names) if n not in new and n not in changed]
        for n in rest:
            show(n, "未变")

    if missing_remote:
        print("\n飞书缺（本地有、远端没有）：")
        for n in missing_remote:
            print(f"  - {n}")
    if extra_remote:
        print("\n飞书多（远端有、本地没有，可能你本地删了；脚本不删远端）：")
        for n in extra_remote:
            print(f"  - {n}")

    print("\n" + "-" * 62)
    print(f"GitHub：{git_info['detail']}")
    if feishu_info.get("ok"):
        s = feishu_info.get("summary") or {}
        print(f"飞书  ：上传 {s.get('uploaded', 0)} / 跳过 {s.get('skipped', 0)}"
              f" / 失败 {s.get('failed', 0)}")
    else:
        print(f"飞书  ：失败 —— {feishu_info.get('detail')}")

    # 8) 基线（只在真正推送成功后更新，否则下一轮会漏报）
    if not args.check and (git_info.get("pushed") or args.no_git):
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

    ok = git_info.get("pushed") or args.no_git or args.check
    return 0 if ok and feishu_info.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
