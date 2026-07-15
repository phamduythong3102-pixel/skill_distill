# 项目说明

将 IPRAN 网络运维排障文档批量蒸馏为网管 agent 可用的 skill 文件。

- `skill_self_distill_pipeline.py`：主流水线。扫描源文档树（`result/v01/tree`），按"一级目录/二级目录"分组，调用大模型把每组文档合并转换为一个 skill（输出为 `<一级中文>/<二级中文>.md`），并生成 `skill_tree_structure.txt` 与 `conversion_report.json`。
- `validate_skills.py`：校验生成的 skill 是否完整合规（frontmatter、截断、残留引用、禁用短语等），存在 ERROR 时退出码为 1。

源文档树和生成产物不入库（见 `.gitignore`）。

# Git 工作流

- 所有修改直接基于 `main` 开发，提交后合入（推送到）`main`。
- 不要创建新分支。
