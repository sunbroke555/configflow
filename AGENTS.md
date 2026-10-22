# ConfigFlow 协作指引

## 文档路由

按任务读对应文档，不要整仓扫描：

| 任务 | 先读 |
|---|---|
| 改前端界面、样式、组件、交互、动效，或新增页面 | [DESIGN.md](DESIGN.md) |

## 测试

- 后端全量：`scripts/test-backend.sh`（pytest-xdist 并行；单个文件或调试仍可直接 `python -m pytest backend/test_x.py`）。
- 前端全量：`cd frontend && npm test`。
