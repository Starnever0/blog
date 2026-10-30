# blogsite

单页 HTML 博客。一页一文件，平铺在本目录，自包含。
站点 https://starnever0.github.io/blog/ · 仓库 `Starnever0/blog`

## 写一篇

根目录新建 `<YYYY-MM-DD>-<slug>.html`，写完整 HTML 文档，样式内联在 `<style>` 里。

**不要加 YAML front matter**——加了会被当模板文件处理。图片用外链或 data URI。

## 发布

```bash
python sync_blog.py
```

提交推 GitHub + 镜像飞书 + 出日报（本地路径 / GitHub URL / 飞书 URL）。
不要手动 `git push`：那样飞书和同步基线会对不上。

## 不要动

- `index.html` — 脚本从清单重新生成，手改会被覆盖
- `collections/manifest.yaml` 里的 `collection` 字段 — 归属由人判断
- `.state/` — 同步基线，改了会重复上报

## 不要做

- 不建栏目、不打标签、不猜归属。分类是攒够一批之后由人来定的事
- 不删文件。本地删了远端保留是故意的，不是 bug
- 不套子目录做层级。站点根已经是 `/blog/`，再套一层就变成 `/blog/blog/`
