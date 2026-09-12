# web-legacy：旧版控制台（冻结备份）

本目录是前端重构前的旧版 Web 控制台（单文件 `src/App.tsx` + `src/components/PageGraphView.tsx`），
仅作为历史实现参考被冻结保留，**不再维护、不再构建、不参与任何门禁检查**。

- 现行前端位于 `../web/`，为完全重写的版本，架构与设计说明见 `../web/README.md`。
- 旧版的接口调用方式仍可作为后端 API 用法的参考，但字段以 `../web/src/api/types.ts` 为准。
- 请勿在本目录继续开发；如需回滚到旧界面，请使用 Git 历史而非在此修改。
