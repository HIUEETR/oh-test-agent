// 应用入口：挂载 React 根组件并引入全局样式与 React Flow 基础样式。

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@xyflow/react/dist/style.css";
import "./styles/tokens.css";
import "./styles/global.css";
import App from "./app/App";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
