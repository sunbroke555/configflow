# Host 网络内部端口冲突回归

基线：`upstream/main` 的 `6625c51a`。应用修复提交：`5f09c25e`。
所有测试、镜像构建在测试服务器执行，使用临时容器和空数据目录。

## 验证结果

- 精确后端回归：`test_web_listener.py`、`test_http_server.py`、`test_dependencies.py`、`test_sub_store_runtime.py`、`test_sub_store_no_flow.py`，125 passed。
- 真实 Nginx：9 项通过，覆盖 IPv4 / IPv6、修改端口、占用拒绝、配置错误回滚、保存失败回滚和重启。
- 真实 Docker Host：同时占用默认 5001 / 3001，改用三个独立高端口。6 个页面 API、IPv6 API、内置 Sub-Store 的真实 Snell 格式转换、容器重启全部通过。
- 全量后端：10680 passed、122 skipped、6 failed。六项在独立解压的原始 `6625c51a` 上同样失败，与本次端口修改无关。

既有失败均为旧安装脚本快照 / SHA 未更新：

- `test_router_split_regression.py::test_single_service_scripts_match_the_previous_release`
- `test_gateway_core_install.py::test_selective_gateway_script_is_byte_identical_to_the_previous_release`
- `test_gateway_core_install.py::test_ordinary_aio_script_is_the_previous_release_plus_intentional_forwarding_context_delta`
- `test_standalone_forwarding_install.py::test_opt_out_is_byte_identical_to_the_previous_release`
- `test_standalone_forwarding_install.py::test_dns_services_never_take_forwarding` 的两组参数。

## 重复执行

```sh
rt -- /home/sunbroke/cf-test/.venv/bin/python -m pytest \
  backend/test_web_listener.py backend/test_http_server.py \
  backend/test_dependencies.py backend/test_sub_store_runtime.py \
  backend/test_sub_store_no_flow.py -q
rt -- python3 scripts/tests/web_listener_integration.py
rt -- python3 scripts/tests/host_ports_integration.py \
  --base-image thsrite/config-flow@sha256:0e5ce89b604f89a123152b6f37f83addc6004259ff179b3266800f55de5a5730 \
  --report /tmp/configflow-host-ports-report.json
```

Docker 脚本在官方基线镜像上复制本次后端与 Nginx 文件构建最小修复镜像；它不重建前端或 Agent，不挂载生产数据，结束后移除测试容器，保留镜像供检查。需要 Linux、sudo Docker 和 IPv6 回环支持。
