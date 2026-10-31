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

一次做完四件事：提交推 GitHub → 镜像飞书云空间「博客网页」→ 在飞书知识库「博客」补一个 shortcut 节点 → 出日报。

日报每条给五个坐标：本地路径 / 站点 URL / 仓库 URL / 飞书文件 URL / 知识库节点 URL。

不要手动 `git push`：那样飞书和同步基线会对不上。

## 不要动

- `index.html` — 脚本从清单重新生成，手改会被覆盖
- `collections/manifest.yaml` 里的 `collection` 字段 — 归属由人判断
- `.state/` — 同步基线，改了会重复上报
- 飞书知识库里的节点顺序 — 脚本只增节点，不排序不挪动

## 不要做

- 不建栏目、不打标签、不猜归属。分类是攒够一批之后由人来定的事
- 不删远端文件。本地删了，飞书会留着（GitHub 那边 `git add -A` 会跟着删，这是 git 的行为）
- 不套子目录做层级。站点根已经是 `/blog/`，再套一层就变成 `/blog/blog/`
- 不在知识库里建子层。节点一律平铺在空间根

## 知识库去重

脚本读飞书知识库的实时节点列表，按 `obj_token`（= 云空间文件 token）判断是否已挂过。
**不依赖本地状态文件**：状态丢了、换机器了也不会重复建。
