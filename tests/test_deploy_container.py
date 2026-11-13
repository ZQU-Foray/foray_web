# -*- coding: utf-8 -*-
"""NAS 容器发布配置的自检（2026-11-13）。

容器镜像无法在开发机构建，所以这里用源码级断言把「改坏了会出事」的约定钉住：
单 worker、配置与固件走挂载、镜像不带测试与本地目录、健康检查打的是未登录可访问的页面。
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ContainerReleaseTest(unittest.TestCase):
    def test_dockerfile_keeps_single_worker_and_healthcheck(self):
        text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        # 单 worker：会话、镜像缓存、限频与考勤内存状态都在进程内
        self.assertIn("--workers 1", text)
        # 健康检查走未登录可访问的登录页，而不是受保护的接口
        self.assertIn("HEALTHCHECK", text)
        self.assertIn("/login", text)
        # 运行期配置目录只建占位，真实内容由宿主机挂载
        self.assertIn("mkdir -p config firmware tls", text)

    def test_dockerfile_sources_exist(self):
        text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        for name in ("app", "static", "scripts", "docs", "requirements.txt", "README.md", "run.sh"):
            self.assertTrue((ROOT / name).exists(), f"Dockerfile 需要 {name}，但仓库里没有")
        # 前端产物必须随镜像走：服务器与 NAS 都不装 node
        self.assertIn("COPY static ./static", text)

    def test_dockerignore_excludes_runtime_state(self):
        lines = [line.strip() for line in
                 (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()]
        for item in ("config", "firmware", "tls", "tests", "backups", ".venv", "web"):
            self.assertIn(item, lines)
        # 前端构建产物要给镜像用，不能被整体排除
        self.assertNotIn("static", lines)

    def test_compose_mounts_config_and_maps_port(self):
        text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        for needle in ("./config:/app/config", "./firmware:/app/firmware", "./tls:/app/tls",
                       "restart: unless-stopped", "env_file"):
            self.assertIn(needle, text)
        self.assertIn(":8000", text)

    def test_env_sample_has_feishu_keys(self):
        text = (ROOT / "deploy" / "nas.env.example").read_text(encoding="utf-8")
        for key in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_REDIRECT_URL",
                    "FEISHU_ALLOWED_TENANT_KEYS", "SCAN_SUBNET"):
            self.assertIn(key, text)
        # 样例里不出现真实密钥
        self.assertNotIn("cli_", text.replace("cli_xxxxxxxxxxxxxxxx", ""))

    def test_base_image_is_configurable(self):
        """NAS 的镜像加速器可能对 docker.io 返 401：基础镜像必须能用参数换掉，而不是改文件。"""
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ARG PYTHON_IMAGE=python:3.10-slim", dockerfile)
        self.assertIn("FROM ${PYTHON_IMAGE}", dockerfile)
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn("PYTHON_IMAGE", compose)
        env_sample = (ROOT / "deploy" / "nas.env.example").read_text(encoding="utf-8")
        self.assertIn("PYTHON_IMAGE", env_sample)
        doc = (ROOT / "docs" / "portal-nas-deploy.md").read_text(encoding="utf-8")
        self.assertIn("registry-mirrors", doc)

    def test_nas_deploy_doc_covers_feishu_and_limits(self):
        text = (ROOT / "docs" / "portal-nas-deploy.md").read_text(encoding="utf-8")
        for needle in ("重定向 URL", "tenant_key", "/onboarding", "SCAN_SUBNET",
                       "deploy.py", "单 worker"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
