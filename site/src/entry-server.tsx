import React from "react";
import { renderToString } from "react-dom/server";
import { ConfigProvider } from "antd";
import { createCache, extractStyle, StyleProvider } from "@ant-design/cssinjs";
import App from "./App";

/**
 * Rendered at build time into static HTML: crawlers and text browsers get the
 * full page, the client bundle only hydrates it.
 */
export function render(): { html: string; style: string } {
  const cache = createCache();
  const html = renderToString(
    <StyleProvider cache={cache}>
      <ConfigProvider
        theme={{
          token: {
            colorPrimary: "#1677ff",
            borderRadius: 8,
            fontFamily:
              '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
          },
        }}
      >
        <App />
      </ConfigProvider>
    </StyleProvider>,
  );
  return { html, style: extractStyle(cache, true) };
}
