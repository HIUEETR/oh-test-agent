// vitest 全局 setup：接入 Testing Library 的 DOM 断言与自动清理。

import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
});
