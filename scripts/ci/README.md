# 回归检查与测试分片

这里面向项目维护者。普通安装无需运行测试，部署步骤见 [README](../../README.md#快速开始)。命令与当前 [checks.yml](../../.github/workflows/checks.yml) 和 [分片工具](shard_tests.py) 对齐。

## 工作流何时运行

`checks.yml` 当前提供 `workflow_dispatch`（手动运行）和 `workflow_call`（供其他工作流调用），没有声明 `pull_request` 触发。不能仅因打开 PR 就认为整套回归已自动运行，应查看实际 Actions 记录。

同一工作流和 ref 的回归不会取消已运行任务，`cancel-in-progress: false`。Docker 镜像构建有自己的触发路径和取消策略，不代表回归工作流的行为。

## 检查范围

| Job | 执行内容 |
| --- | --- |
| `backend-tests` | 2 个分片，pytest 收集完整后端测试，每片 4 个 xdist worker |
| `backend` | 等待两片成功，再验证所有测试恰好出现一次 |
| `frontend-tests` | 2 个 Vitest 分片，保存实际测试报告 |
| `frontend-build` | 收集完整测试文件清单，执行 TypeScript 检查与生产构建 |
| `frontend` | 等待分片和构建成功，验证实际报告覆盖完整文件清单 |
| `agent` | 按 Agent 的 go.mod 安装 Go，运行 `go test ./...` |
| `node-probe` | 按测速组件的 go.mod 安装 Go，运行竞态检查和构建 |

工作流不按改动路径挑选部分测试。报告保留 7 天。实际保护规则和必需检查由 GitHub 仓库设置决定，不能从文件内容推断。

## 本地准备

以下 Python 命令在仓库根目录执行。使用 Python 3.11 或兼容版本，先创建虚拟环境，再安装后端依赖及测试依赖：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r backend/requirements.txt pytest pytest-subtests pytest-xdist
```

前端 package.json 要求 Node.js 20 及以上，当前 CI 使用 Node.js 24。Go 版本分别以 [Agent go.mod](../../backend/agents/go-agent/go.mod) 和 [测速组件 go.mod](../../backend/node-probe/go.mod) 为准。

## 先运行完整检查

后端使用单独的临时数据目录，不要指向正在使用的面板数据：

```bash
CF_TEST_DATA_DIR="$(mktemp -d)"
DATA_DIR="$CF_TEST_DATA_DIR" python -m pytest backend -q -n 4 --dist=worksteal
```

前端在 `frontend/` 中执行：

```bash
cd frontend
npm ci
npm test
npm run typecheck
npm run build
```

Agent 在 `backend/agents/go-agent/` 中执行 `go test ./...`。测速组件在 `backend/node-probe/` 中执行 `go test -race ./...` 和 `CGO_ENABLED=0 go build -trimpath -o configflow-node-probe .`。真实核心集成测试若需要额外二进制，应按相应测试的环境变量说明准备；跳过不能算作通过。

## 后端如何分片

工具先在独立 pytest 进程收集全部测试，再按文件分配。历史耗时来自 `.github/ci/backend-durations.json`，较慢的文件优先放入当前负载较低的分片；新增文件仍会被收集，采用已知文件耗时的中位数。耗时只影响分配，不用于排除测试。

在仓库根目录运行第一片：

```bash
CF_TEST_DATA_DIR="$(mktemp -d)"
DATA_DIR="$CF_TEST_DATA_DIR" python scripts/ci/shard_tests.py backend --shard 1/2 \
  --durations .github/ci/backend-durations.json --plan .ci-results/backend-1.json \
  -- -q -n 4 --dist=worksteal --durations=15 --junitxml=.ci-results/backend-1.xml
```

第二片改为 `2/2`，计划和报告名称也改为 `backend-2`。`--plan-only` 只生成计划；`--` 后的参数转发给 pytest，不要添加筛选、排除或额外测试路径，否则不能证明完整覆盖。

两片执行后验证：

```bash
python scripts/ci/shard_tests.py validate \
  --expected .ci-results/backend-1.json --expected-key all_nodeids \
  --inventory-key all_nodeids --shard-key selected_nodeids \
  .ci-results/backend-1.json .ci-results/backend-2.json
```

## 前端如何验证完整覆盖

在 `frontend/` 中先收集完整文件清单，再执行两片：

```bash
mkdir -p ../.ci-results
npx vitest list --filesOnly --json > ../.ci-results/frontend-all.json
npm test -- --shard=1/2 --reporter=default --reporter=json --outputFile=../.ci-results/frontend-1.json
npm test -- --shard=2/2 --reporter=default --reporter=json --outputFile=../.ci-results/frontend-2.json
```

返回仓库根目录后验证：

```bash
python scripts/ci/shard_tests.py validate \
  --expected .ci-results/frontend-all.json --expected-key file \
  --shard-key testResults.name --path-prefix frontend/tests \
  .ci-results/frontend-1.json .ci-results/frontend-2.json
```

Vitest 3.2.6 的 `list` 不按 `--shard` 切分，不能拿两份 list 输出当作实际运行报告。验证器会拒绝空清单、重复、跨片重叠、缺失和多余测试；`--inventory-key` 还能检查各片是否收集到同一完整清单。

## 分片工具自身的回归

安装前述依赖后，在根目录执行：

```bash
python -m unittest discover -s scripts/ci -p 'test_*.py'
```

## 更新历史耗时

测试规模明显变化时，可以在隔离数据目录中重新测量。下面保留生成耗时 JSON 的完整步骤，执行会修改历史耗时文件，修改后应检查差异再提交。

```sh
CF_TEST_DATA_DIR="$(mktemp -d)"
DATA_DIR="$CF_TEST_DATA_DIR" python -m pytest backend -q -n 4 --dist=worksteal --junitxml=/tmp/backend-timings.xml
python - <<'PY'
import json
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET

durations = defaultdict(float)
for case in ET.parse('/tmp/backend-timings.xml').iter('testcase'):
    parts = case.attrib['classname'].split('.')
    for length in range(len(parts), 0, -1):
        path = Path(*parts[:length]).with_suffix('.py')
        if path.is_file():
            durations[path.as_posix()] += float(case.attrib.get('time', 0))
            break
    else:
        raise ValueError(f'Cannot map test to a file: {case.attrib}')
Path('.github/ci/backend-durations.json').write_text(
    json.dumps({path: round(seconds, 3) for path, seconds in sorted(durations.items())}, indent=2) + '\n'
)
PY
```

耗时是每项测试时间之和，不等于 4 个并行 worker 下的墙钟时间。删除文件会被忽略，新测试仍以完整收集结果为准。
