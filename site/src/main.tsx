import React from "react";
import { hydrateRoot } from "react-dom/client";
import { ConfigProvider } from "antd";
import App from "./App";
import "./styles.css";

hydrateRoot(
  document.getElementById("root")!,
  <React.StrictMode>
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
  </React.StrictMode>,
);
