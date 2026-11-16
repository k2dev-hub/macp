# 项目官网（纯静态）

介绍 MACP 的单页站点。构建时把 React + antd 渲染成静态 HTML，因此：

- 爬虫和纯文本浏览器不需要执行 JS 就能看到全部内容；
- 线上只需要一个静态文件服务器，没有 Node 运行时；
- 首屏没有外部字体和第三方脚本，只有 antd 的样式和一小段 hydration JS。

## 构建

Node 只在构建时需要：

```bash
docker run --rm -v "$PWD/site:/app" -v macp-site-node:/app/node_modules \
  -w /app node:22-alpine sh -c "npm install && npm run build:all"
```

产物在 `site/dist/client/`：`index.html`（已预渲染）、`assets/`、`sitemap.xml`、`robots.txt`。

## 本地预览

```bash
docker run --rm -p 8090:80 -v "$PWD/site/dist/client:/usr/share/nginx/html:ro" nginx:1.27-alpine
```

## 部署

```bash
docker compose -f deploy/site/docker-compose.yml up -d
# 默认只发布在 127.0.0.1:8090；要对外就在 compose 里改成主机地址
```

## 上线前的检查

- `deploy/site/nginx.conf` 关了目录列举与版本号，并加了 CSP / X-Content-Type-Options /
  X-Frame-Options / Referrer-Policy / Permissions-Policy；
- HTML 不缓存（`no-cache`），`assets/` 是带哈希的不可变资源（一年）；
- 站点里不出现任何内网地址、密钥路径或真实构建编号，示例编号统一用 `B-20260101-EXAMPLE`；
- 用 Cloudflare Tunnel 发布时，TLS 与 HSTS 在 Cloudflare 侧配置，源站只监听本机端口。

## SEO 清单

| 项 | 位置 |
|---|---|
| 标题、描述、关键词 | `index.html` 的 `<title>` / `meta` |
| 规范地址 | `<link rel="canonical">` |
| 社交卡片 | Open Graph + Twitter Card |
| 结构化数据 | `SoftwareApplication`、`BreadcrumbList`（JSON-LD） |
| 语义结构 | 唯一 `h1`、分节 `h2`/`h3`、`nav`/`main`/`footer` |
| 抓取入口 | `sitemap.xml`、`robots.txt` |
| 无 JS 可读 | 预渲染的完整 HTML（可见文本约 2.8 KB） |

改文案后重新构建即可，`sitemap.xml` 的 `lastmod` 会自动更新为构建日期。
