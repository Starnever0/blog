# blogsite

单页 HTML 博客。一页一文件，平铺在本目录，自包含。
站点 https://starnever0.github.io/blog/ · 仓库 `Starnever0/blog`
替换旧版 blogkit（已停止使用）

## 写一篇

根目录新建 `<YYYY-MM-DD>-<slug>.html`，写完整 HTML 文档，样式内联在 `<style>` 里。

**不要加 YAML front matter**——加了会被当模板文件处理。图片用外链或 data URI。

## 发布

```bash
python sync_blog.py
```

直接这么敲就行。PATH 上的 python 缺 pyyaml 时，脚本会自己换一个能用的解释器重跑。

一次做完：提交推 GitHub → 上传云空间 → 在知识库「博客」补一个 shortcut 节点 → 出日报。

日报每条给：本地路径 / 站点 URL / 仓库 URL / 知识库节点 URL，外加一行各通道状态
（`GitHub ✓  云空间 ✓  知识库 ✓`）。**云空间不是目的地**，只是知识库节点的载体，
不用管它。

不要手动 `git push`：那样状态判断会和远端对不上。

## 不要动

- `index.html` — 脚本从清单重新生成，手改会被覆盖
- `collections/manifest.yaml` 里的 `collection` 字段 — 归属由人判断
- `.state/` — 同步状态，改了会重复上报
- 飞书知识库里的节点顺序 — 脚本只增节点，不排序不挪动

## 不要做

- 不建栏目、不打标签、不猜归属。分类是攒够一批之后由人来定的事
- 不删远端文件。本地删了，飞书会留着（GitHub 那边 `git add -A` 会跟着删，这是 git 的行为）
- 不套子目录做层级。站点根已经是 `/blog/`，再套一层就变成 `/blog/blog/`
- 不在知识库里建子层。节点一律平铺在空间根

## 只做本地构建时

```bash
python sync_blog.py --build-collections --no-git --no-feishu --no-state
```

`--no-state` 保证完全不碰 `.state/`。不加时，`seen` 快照仍会更新——它只记
「这个内容见过」，不代表已同步，所以不会掩盖未同步的问题。

## 状态是怎么判的

没有一个「已同步」总标记，每通道各算各的：

| 通道 | 判定依据 |
|---|---|
| GitHub | `git diff --quiet origin/main -- <file>`，不需要存状态 |
| 云空间 | 上次上传 `failed == 0` 且文件在远端时记下的 sha |
| 知识库 | 实时 `wiki +node-list` 里有没有 obj_token 匹配的节点 |

所以关掉某个通道，绝不会把文件标成已同步——这是旧版的坑。
知识库去重也因此不依赖本地状态文件：状态丢了、换机器了也不会重复建。
