"""一键回归：起测试服务 → API 契约 → UI 端到端 → 汇总退出码。"""
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import harness


def main():
    harness.ensure_browser_env()
    print("启动测试服务…")
    if not harness.start_service():
        print("✗ 测试服务未就绪")
        return 1
    total_fail = 0
    try:
        for mod_name in ("test_api", "test_transports", "test_flash", "test_single_sta", "test_ui", "test_portal", "test_mobile", "test_typography", "test_portal_sync", "test_portal_cache", "test_auth", "test_acceptance", "test_backup_restore", "test_ops", "test_accounts", "test_identity_d2", "test_tls", "test_security_review"):
            print(f"\n======== {mod_name} ========")
            try:
                mod = __import__(mod_name)
                if mod_name in ("test_ui", "test_portal", "test_mobile", "test_typography"):
                    import asyncio
                    npass, nfail = asyncio.run(mod.run_all())
                    total_fail += nfail
                else:
                    mod.run()
                    total_fail += len(mod.FAIL)
            except Exception:
                traceback.print_exc()
                total_fail += 1
    finally:
        harness.stop_service()
    print(f"\n==== 汇总: 失败 {total_fail} ====")
    return 1 if total_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
