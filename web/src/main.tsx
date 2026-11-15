import React from "react";
import ReactDOM from "react-dom/client";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import HumanApp from "./HumanApp";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: "#2563eb",
          borderRadius: 10,
          colorBgLayout: "#f5f7fb",
          colorText: "#1f2937",
          colorTextSecondary: "#667085",
          fontFamily:
            '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
        },
        components: {
          Card: { headerFontSize: 14 },
          Table: { headerBg: "#f9fafb", headerColor: "#475467" },
          Menu: { itemBorderRadius: 8, itemMarginInline: 8 },
        },
      }}
    >
      <HumanApp />
    </ConfigProvider>
  </React.StrictMode>,
);