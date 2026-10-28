# 把使用手册发布到 GitHub Pages

GitHub Pages 可以把仓库里的手册变成带目录和搜索的公开网页。仓库已经提供自动发布配置，**首次仍需有管理或维护权限的人启用 Pages**；设置完成后，文档更新合并到 `main` 就会自动发布。

这是使用手册网站，不是 ConfigFlow 管理面板。开启它不会把家里的面板、订阅数据或设备管理服务搬到 GitHub。

## 作者合并后需要做什么

### 1. 开启 Pages

1. 打开 GitHub 上的 **thsrite/configflow** 仓库。
2. 进入 **Settings → Pages**。
3. 在 **Build and deployment → Source** 选择 **GitHub Actions**。

仓库已带 `.github/workflows/docs.yml`，不必再选择 GitHub 推荐的模板，也不必建立 `gh-pages` 分支。这里使用自动构建的结果发布，不能把 `doc/` 或 `docs/` 目录直接选成分支发布来源。

如果看不到 Settings，或无法修改 Pages，请让有仓库管理或维护权限的人操作。各账号与组织的界面权限可能不同。

### 2. 确认 Actions 和发布环境允许运行

| 在哪里检查 | 需要满足什么 | 通常怎么做 |
| --- | --- | --- |
| **Settings → Actions → General** | 仓库允许运行 GitHub Actions | 已正常运行其他工作流时，通常不必修改 |
| Actions 的允许列表 | 能使用本工作流调用的 GitHub 官方操作 | 若仓库或组织限制可用操作，允许需要的 `actions/*` 操作；具体名称见下方 |
| **Settings → Environments → github-pages** | 发布分支规则允许 `main` | 若已有保护规则，确认 `main` 没被排除；环境还不存在时，会在发布流程使用它时创建 |
| `github-pages` 的审批要求 | 有审批要求时，发布会等待指定人员批准 | 希望每次自动上线时，不额外设置人工审批；已有团队要求则按原规则批准 |

工作流使用 `actions/checkout@v6`、`actions/setup-node@v6`、`actions/configure-pages@v5`、`actions/upload-pages-artifact@v4`、`actions/deploy-pages@v4`。上传操作还会调用 GitHub 的文件上传操作；遇到允许列表报错时，按日志提示核对被阻止的操作。

**不用创建个人访问令牌（PAT），也不用新增 Secrets。** 工作流使用 GitHub 自动提供的 `GITHUB_TOKEN`，并分别声明读取、发布所需的权限；无需为此把整个仓库的默认 Workflow permissions 改成读写。若组织政策禁止相关权限，需要由组织管理员调整限制。

### 3. 手动运行首次发布

1. 打开仓库顶部 **Actions**。
2. 在左侧选择 **Documentation (GitHub Pages)**。
3. 点击 **Run workflow**，分支选 **main**，再确认运行。
4. 等待 **Build and check documentation** 和 **Publish documentation** 都成功。若显示等待审批，按上一节检查环境要求。

这个手动入口在工作流合并到默认分支后才会出现。当前配置仅允许从 `main` 发布，选择其他分支不会部署。

如果合并时还没启用 Pages，合并触发的第一次运行可能已经失败。先完成上述设置，再从 `main` 手动运行一次即可，不需要为了触发发布重新修改文档或提交空改动。

### 4. 打开正式网址

发布成功后，在 **Settings → Pages** 或那次成功运行的部署结果里，点击 GitHub 显示的网站地址。**以那里显示的实际地址为准。**

未配置自定义域名、仓库仍名为 `configflow` 时，上游仓库的默认地址预计为 [thsrite.github.io/configflow](https://thsrite.github.io/configflow/)。这是成功发布后的地址说明，并不表示当前已经上线。

如果部署在自己的 fork 上，网址使用自己的 GitHub 账号，例如 `https://你的账号.github.io/configflow/`。推送到 fork 不会更新作者仓库的网站。

## 以后怎样更新

| 操作 | 会发生什么 |
| --- | --- |
| 向 `main` 提交或合并 `doc/`、`scripts/docs/`、`.github/workflows/docs.yml` 的改动 | 自动构建，检查页面、图片和段落链接，通过后发布 |
| 对 `main` 提交相关 PR | 只构建并检查，不上传发布文件、不部署，也不要求先启用 Pages |
| 只修改应用代码或根目录 README | 不触发这份文档工作流；需要重发时可手动运行 |
| 在 Actions 手动运行并选择 `main` | 按 `main` 的当前内容重新构建和发布 |
| 构建或检查失败 | 不进入发布步骤；先查看失败日志并修复 |

构建使用 Node.js 24 和 `doc/package-lock.json` 中锁定的依赖。上传范围只有 `doc/.vitepress/dist/`，不上传整个仓库，也不需要把生成文件提交回 Git。

## 改仓库名或使用自己的域名

正式发布时，工作流会从 GitHub Pages 读取实际网址路径，再交给文档构建。例如项目站点使用 `/configflow/`，自己的域名通常使用 `/`。不用为上游和 fork 分别手改侧栏、图片或搜索地址。

改仓库名、配置自定义域名或改变 Pages 地址后，在 **Actions → Documentation (GitHub Pages)** 从 `main` 重新运行一次，让构建使用新路径。自定义域名还需在 **Settings → Pages → Custom domain** 配置，并按 GitHub 提示完成域名的 DNS 设置；只改文档里的网址不会完成域名接入。

## 发布失败时看哪里

| 现象 | 怎么处理 |
| --- | --- |
| 找不到这个工作流或 Run workflow | 确认配置已合并到默认分支、Actions 已启用，并确认当前账号有运行权限 |
| `Read GitHub Pages settings` 提示找不到 Pages / 404 | 到 Settings → Pages 选择 GitHub Actions，再从 `main` 重跑 |
| 提示某个 Action 不被允许 | 核对仓库或组织的 Actions 允许列表，允许日志指出的 GitHub 官方操作 |
| 提示权限不足或 token 权限错误 | 核对 Actions、组织政策及工作流声明的权限；不要先改用个人令牌绕过问题 |
| 等待环境批准，或提示部署分支受限 | 检查 `github-pages` 环境，批准该次部署或允许 `main` |
| 构建或链接检查失败 | 在失败步骤中找到具体文档、图片或段落链接，修复后提交；不会因为手动重跑就自动修好 |
| 网站打开是 404 | 先确认发布成功，再使用 Pages 设置页给出的完整地址；首次发布后可稍等再刷新 |
| 首页能打开，图片或其他页面出错 | 核对是否刚改过仓库名或域名；从 `main` 重跑生成正确路径，并检查部署日志 |

仅在测试服务器运行 `npm --prefix doc run build` 不会更新 GitHub Pages。正式网站由仓库工作流发布，测试服务器预览与它是两套独立站点。

相关依据：[GitHub Pages 发布来源设置](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site)、[GitHub Pages 自定义工作流](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)。
