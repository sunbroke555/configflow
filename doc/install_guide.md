# 安装与入门指南

普通 Docker / NAS 主机按下文操作。RouterOS Container 请阅读 [ConfigFlow + RouterOS](../docs/CONFIGFLOW_AGENT_ROUTEROS_INSTALL.md)。

## 启动面板

1. 安装 Docker 和 Docker Compose，准备一个长期保存数据的目录。
2. 在该目录新建 `compose.yaml`，粘贴下面内容，替换登录密码和随机密钥。

```yaml
services:
  config-flow:
    image: thsrite/config-flow:latest
    container_name: config-flow
    ports:
      - "5010:80"
    volumes:
      - ./data:/data
    environment:
      DATA_DIR: /data
      TZ: Asia/Shanghai
      ADMIN_USERNAME: admin
      ADMIN_PASSWORD: "replace-with-a-strong-password"
      JWT_SECRET_KEY: "replace-with-at-least-32-random-characters"
    restart: unless-stopped
```

3. 在文件所在目录执行：

```bash
docker compose config --quiet
docker compose pull
docker compose up -d
```

4. 当前电脑访问 `http://localhost:5010`；NAS 或服务器使用 `http://设备IP:5010`。
5. 登录后在「系统设置」填写设备能访问的「服务域名」，生成并保存「访问令牌」。

镜像已内置 Sub-Store 和节点测速组件。Sub-Store 默认按需启动，休眠时会在转换任务中自动唤醒，无需单独安装。

## 完成第一份配置

按顺序操作：新建并选择配置空间 → 添加订阅并手动更新 → 创建引用订阅的策略组 → 添加规则并把 `MATCH` 排在最后 → 生成、下载并导入客户端。

每一步的填写示例见 [README 第一次使用](../README.md#第一次使用)。手动节点在策略组的「节点」来源中选择；共享资源不会自动加入每个新配置。

## 后续维护

- 部署文件和 `data` 目录需要长期保留。重装或换主机前，停服复制完整数据目录。
- 修改部署文件中的环境变量后执行 `docker compose up -d`，仅重启容器不会应用修改。
- 更新镜像前记录当前版本并确认备份成功，不只备份 `configflow.sqlite3` 或根 `config.json`。
- 完整部署参数、故障排查和升级命令见 [部署指南](module/deployment.md)。

## 实现参考

[Compose 配置](../docker/docker-compose.yml)、[页面导航](../frontend/src/navigation.ts)、[配置仓库](../backend/common/config_repository.py)。
