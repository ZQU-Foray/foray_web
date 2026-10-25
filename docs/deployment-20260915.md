# 2026-09-15 全量部署记录

## 结果

- 入口：http://portal.example.com；SSH 端口 <SSH端口>。
- 应用目录：/opt/portal；portal.service 正常运行，已启用开机启动。
- 全量回归：tests/e2e/run_all.py，汇总失败 0；移动端 82 项通过，安全回归 30 项通过。
- 部署包：175 文件，1.7 MB；受保护路径校验与单进程校验通过。
- 107 个应用、静态资源、脚本及部署文件的 SHA-256 与本地一致。
- 公网探针：/login 200，/portal/portal.css 200，/api/portal/overview 401，/portal/fixtures.js 404。
- Python 全部运行依赖可导入，pip check 无冲突。

## 首次安装

服务器原目录为未完成安装，无服务单元、无业务配置，虚拟环境缺少 pip。完成系统与应用依赖安装，用本地配置、账号库及一致性快照初始化，未迁移登录会话和旧应急账号。环境文件权限 600，飞书回调改为 http://portal.example.com/auth/feishu/callback。

修复上游 DNS 解析异常：enp2s0 使用 223.5.5.5、1.1.1.1，已保存到 /etc/netplan/02-lan.yaml，并通过 netplan generate；运行中的解析已恢复。

## 配套服务

- feishu-member-sync.service 已启动并开机自启，首轮 5 名成员事项统计成功，failures=0；考勤接口返回空记录，不代表缺勤。
- 门户同步健康：projects=3、tasks=8、milestones=3、schedule=3、members=5、roster=5、quality_issues=0，last_error 为空，数据库完整性正常。
- portal-backup.timer、portal-health.timer 已启用；手动执行对应服务均成功，退出码 0。
- USB OpenOCD 0.11.0 已安装，设备规则已安装并重新加载。
- TCP OpenOCD 0.12.0+dev 安装于 /opt/openocd-vllink，源码提交 0e0f415b6f1f7e9997e4b30206cfb3fcf5287e52；应用使用的 TCP 参数解析成功。
- 未执行真实硬件连接、固件烧录或人工飞书登录。

## 备份与恢复边界

- 安装前目录及网络配置：/opt/portal-backups/pre-full-20260915-173020/。
- 部署前数据备份：/opt/portal-backups/20260915-173607/。
- 已验证的完整数据备份：/opt/portal-backups/daily-20260915-093737，另有同名 .tar.gz。
- 本机没有可用的旧 webserver.service，不能用 deploy.py --rollback 当作已验证回退方案。数据恢复使用项目 scripts/portal_restore.py；安装前目录备份仅能恢复到未完成安装状态。

## 本地日志

- backups/full-deploy-20260915-tests.log
- backups/full-deploy-20260915-release.log
- backups/full-deploy-20260915-bootstrap-retry.log
- backups/full-deploy-20260915-python.log
- backups/full-deploy-20260915-openocd-build-retry.log
