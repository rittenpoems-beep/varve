## 记忆系统（Varve 分层记忆栈）

本机已安装 Varve。安装路径：`<VARVE_HOME>`；数据目录：`<VARVE_DATA>`。

- **开工**：读**全局卡** `<VARVE_DATA>\STATUS.md` 的「工程状态区」（hook 会自动注入；未注入时手动读）。
- **回溯**：用户提到「上次 / 之前 / 当时 / 曾经 / 那个坑」这类指代时，**先检索再回答**：
  `python -X utf8 "<VARVE_HOME>\scripts\recall.py" "关键词1" "关键词2"`
  检索不到时直说「没找到」，**不要编造**。
- **收尾**：更新**全局卡** `<VARVE_DATA>\STATUS.md` 的任务区（目标 / 进行中 / 下一步 / 待决策 / 最近完成 ≤5 条）。
- **环境或工具异常**：读 `<VARVE_DATA>\ENVIRONMENT.md`，并跑 `pwsh -NoProfile -File <VARVE_HOME>\scripts\doctor.ps1`。
