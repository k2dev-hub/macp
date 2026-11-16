// Prerender: turn the React tree into static HTML so the page is indexable
// without JavaScript, then write sitemap.xml and robots.txt next to it.
//
// The output is a plain directory (dist/) served by any static server: no Node
// runtime is needed in production.

import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");
const clientDir = path.join(root, "dist", "client");
const serverEntry = path.join(root, "dist", "server", "entry-server.js");

const { render } = await import(pathToFileURL(serverEntry).href);
const { html, style } = render();

const template = await readFile(path.join(clientDir, "index.html"), "utf8");

// The client bundle keeps its hashed asset names; only the root markup and the
// extracted antd styles are injected.
const page = template
  .replace("<!--app-html-->", html)
  .replace("</head>", `  <style data-antd>${style}</style>\n  </head>`);

await writeFile(path.join(clientDir, "index.html"), page, "utf8");

const SITE = "https://www.miaodun.tech/";
const today = new Date().toISOString().slice(0, 10);
const sitemap = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>${SITE}</loc>
    <lastmod>${today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
</urlset>
`;
await writeFile(path.join(clientDir, "sitemap.xml"), sitemap, "utf8");
await writeFile(
  path.join(clientDir, "robots.txt"),
  `User-agent: *\nAllow: /\n\nSitemap: ${SITE}sitemap.xml\n`,
  "utf8",
);

const plainText = page
  .replace(/<script[\s\S]*?<\/script>/g, " ")
  .replace(/<style[\s\S]*?<\/style>/g, " ")
  .replace(/<[^>]+>/g, " ")
  .replace(/\s+/g, " ")
  .trim();

console.log(`prerendered: ${html.length} bytes of markup`);
console.log(`headings: h1=${(page.match(/<h1/g) || []).length} h2=${(page.match(/<h2/g) || []).length} h3=${(page.match(/<h3/g) || []).length}`);
console.log(`visible text without JS: ${plainText.length} chars`);
console.log(`sitemap + robots written to ${clientDir}`);
