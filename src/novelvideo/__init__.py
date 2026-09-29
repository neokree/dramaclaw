"""NovelVideo - 小说解说视频自动生成系统。

基于 SuperScript 架构，将小说转换为 30-60 集解说视频。

核心特性：
- SQLite 项目存储，角色/场景/道具直接从原文结构化提取
- 图像：Draw Things（本地）/ Higgsfield；视频：Higgsfield / h3.c（本地）
- 角色一致性保障（多参考图）
- 多 Agent 协作生成
"""
