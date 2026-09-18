// 测试用 Node 内置模块的最小类型声明。
//
// 项目未安装 @types/node，且 tsconfig.app.json 的 types 只显式包含 vite/client，
// 因此 src 下的测试无法拿到 node:fs / node:path 的类型。这里只声明测试实际用到的
// 只读 API，避免为一条样式回归测试引入整套 Node 类型依赖。
//
// 若将来引入 @types/node，请删除本文件并把 "src/types" 从 tsconfig.app.json 的
// types 数组中移除（两者同时存在会重复声明 readFileSync/resolve）。

declare module "node:fs" {
  export function readFileSync(path: string, encoding: "utf8"): string;
}

declare module "node:path" {
  export function resolve(...paths: string[]): string;
}

/** Node 20.11+ / Vite 在 ESM 下提供的当前模块所在目录。 */
interface ImportMeta {
  readonly dirname: string;
}
