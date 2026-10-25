"""飞书多维表格写客户端（新建/更新记录）——仅门户写接口使用。

- 复用 sync 的同步配置、tenant token 缓存与超时/重试策略（同应用身份，
  读侧与写侧共享一份凭据与令牌生命周期）；
- 值形状按官方写契约：日期→毫秒时间戳、人员→[{"id": open_id}]、
  单选→选项名、多选→字符串数组、文本→字符串；
- 失败复用 SyncError 分类（network/rate_limited/api/config），
  由 API 层转成 HTTP 语义返回，绝不伪称成功；
- 端点为同步 def（FastAPI 线程池执行），urllib 同步调用不阻塞事件循环。
"""
import json
import time
import urllib.error
import urllib.request
import uuid

from .. import config
from . import sync
from .sync import SyncError

# token 失效类错误码（与读侧 _get 同一套）
_TOKEN_INVALID = (99991663, 99991661, 99991668)
# 记录不存在（读单条 / 删除时，2026-10-21 实测返回 HTTP 200 + 该码）
RECORD_NOT_FOUND = 1254043


class FeishuWriter:
    """bitable 记录写客户端：create(table_key, fields) / update(...)."""

    def __init__(self):
        self.cfg = sync._load_sync_cfg()
        self._http = sync.FeishuReader(self.cfg)   # 只复用其令牌与 HTTP 细节

    def _table_id(self, table_key):
        tid = (self.cfg["tables"].get(table_key) or {}).get("table_id", "")
        if not tid:
            raise SyncError("config", f"表 {table_key} 未映射（table_id 为空）")
        return tid

    def _call(self, method, path, fields=None, idempotent=False):
        """带写语义的调用：网络/429 退避重试；token 失效强制重取一次。

        idempotent=True：请求带了 client_token（飞书按它去重，2026-10-21 实测同一
        token 重复新建返回同一条记录），新建超时也可以放心重试。

        fields=None 表示无请求体（DELETE）；否则按官方契约包成 {"fields": …}。"""
        body = {"fields": fields} if fields is not None else None
        last = None
        for attempt in range(config.PORTAL_SYNC_RETRIES + 1):
            if attempt:
                time.sleep(config.PORTAL_SYNC_BACKOFF_SEC * attempt)
            try:
                r = self._http._req_json(method, path, body=body)
            except SyncError as e:
                last = e
                # 新建请求超时无法判断上游是否已落库，没有幂等键时自动重发会制造重复记录。
                if method == "POST" and e.klass == "network" and not idempotent:
                    raise SyncError("network", "新建结果尚未确认，请先刷新飞书核对，勿直接重复提交") from e
                if e.klass in ("network", "rate_limited"):
                    continue
                raise
            code = r.get("code")
            if code == 0:
                return r.get("data") or {}
            if code in _TOKEN_INVALID:
                sync.invalidate_token(self.cfg["app_id"])
                self._http._tenant_token(force=True)
                continue
            # 其余业务错误（权限不足/字段类型不符/记录不存在等）立即失败
            raise SyncError("api", f"code={code} {str(r.get('msg'))[:120]}", code=code)
        raise last or SyncError("network", "重试耗尽")

    def create(self, table_key, fields, client_token=None):
        """新建记录，返回 record_id。user_id_type=open_id：人员按 open_id 写入。

        client_token 缺省时自动生成：本次调用内的超时重试不会建出重复记录。
        推送线程传入队列里固定的 token，跨进程重启重推也只会落一条。
        """
        tid = self._table_id(table_key)
        token = client_token or str(uuid.uuid4())
        data = self._call(
            "POST",
            f"/open-apis/bitable/v1/apps/{self.cfg['app_token']}"
            f"/tables/{tid}/records?user_id_type=open_id&client_token={token}",
            fields, idempotent=True)
        return ((data.get("record") or {}).get("record_id")) or ""

    def get(self, table_key, record_id):
        """读单条记录的当前内容：{record_id, fields, last_modified_time, ...}；不存在返回 None。

        带 automatic_fields 取回飞书修改时间；fields 的键是飞书列名（与写入同一套）。
        """
        if not record_id:
            raise SyncError("config", "record_id 为空")
        tid = self._table_id(table_key)
        try:
            data = self._call(
                "GET",
                f"/open-apis/bitable/v1/apps/{self.cfg['app_token']}"
                f"/tables/{tid}/records/{record_id}?user_id_type=open_id&automatic_fields=true")
        except SyncError as e:
            if e.code == RECORD_NOT_FOUND:
                return None
            raise
        return data.get("record") or None

    def update(self, table_key, record_id, fields):
        """按记录 ID 更新指定字段（部分更新，未列出的字段不动）。"""
        if not record_id:
            raise SyncError("config", "record_id 为空")
        tid = self._table_id(table_key)
        base = self.cfg["api_base"]
        self._call(
            "PUT",
            f"/open-apis/bitable/v1/apps/{self.cfg['app_token']}"
            f"/tables/{tid}/records/{record_id}?user_id_type=open_id",
            fields)
        return True

    def delete(self, table_key, record_id):
        """按记录 ID 删除（不可恢复；飞书侧进回收站，由飞书保留策略兜底）。

        记录本来就不存在时返回 False（重推删除不算失败），删掉了返回 True。
        """
        if not record_id:
            raise SyncError("config", "record_id 为空")
        tid = self._table_id(table_key)
        try:
            self._call(
                "DELETE",
                f"/open-apis/bitable/v1/apps/{self.cfg['app_token']}"
                f"/tables/{tid}/records/{record_id}")
        except SyncError as e:
            if e.code == RECORD_NOT_FOUND:
                return False
            raise
        return True

    def upload_attachment(self, data, filename, content_type=""):
        """上传附件文件到本 Base（drive medias/upload_all，≤20MB）→ file_token。

        multipart 用标准库手工拼装（项目不引入 requests）；拿到 file_token 后由
        调用方写入记录的附件字段（值形状 [{"file_token": ...}]，整列替换、
        需先并上既有 token）。parent_type=bitable_file 对应附件字段。
        """
        if not data:
            raise SyncError("api", "上传内容为空")
        boundary = "----portal" + uuid.uuid4().hex
        fields = [("file_name", filename or "upload.bin"),
                  ("parent_type", "bitable_file"),
                  ("parent_node", self.cfg["app_token"]),
                  ("size", str(len(data)))]

        def part(headers, payload):
            out = [("--" + boundary).encode()]
            out += [f'Content-Disposition: form-data; {h}'.encode()
                    for h in headers]
            out += [b"Content-Type: " + (content_type or
                                         "application/octet-stream").encode()]
            return b"\r\n".join(out) + b"\r\n\r\n" + payload + b"\r\n"

        body = b"".join(part([f'name="{k}"'], v.encode())
                        for k, v in fields)
        body += part([f'name="file"; filename="{fields[0][1]}"'], data)
        body += ("--" + boundary + "--\r\n").encode()

        url = (self.cfg["api_base"] + "/open-apis/drive/v1/medias/upload_all")
        last = None
        for attempt in range(config.PORTAL_SYNC_RETRIES + 1):
            if attempt:
                time.sleep(config.PORTAL_SYNC_BACKOFF_SEC * attempt)
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": "multipart/form-data; boundary=" + boundary,
                         "Authorization": "Bearer " + self._http._tenant_token()})
            try:
                with urllib.request.urlopen(
                        req, timeout=config.PORTAL_SYNC_TIMEOUT_SEC) as r:
                    res = json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                if e.code in sync.RETRYABLE_HTTP:
                    last = SyncError("rate_limited" if e.code == 429 else "network",
                                     f"HTTP {e.code}")
                    continue
                try:
                    res = json.loads(e.read().decode())
                except Exception:
                    raise SyncError("api", f"HTTP {e.code}")
            except Exception as e:
                last = SyncError("network", str(e)[:100])
                continue
            code = res.get("code")
            if code == 0:
                token = ((res.get("data") or {}).get("file_token")) or ""
                if token:
                    return token
                raise SyncError("api", "上传成功但未返回 file_token")
            if code in _TOKEN_INVALID:
                sync.invalidate_token(self.cfg["app_id"])
                self._http._tenant_token(force=True)
                continue
            # 常见失败：应用缺「上传文件到云文档」权限（scope）——给出可操作提示
            hint = ("；请到飞书开放平台给自建应用开通『上传、下载文件到云文档』"
                    "权限后重试" if code in (99991672, 99991679, 230002) else "")
            raise SyncError("api", f"code={code} {str(res.get('msg'))[:120]}{hint}")
        raise last or SyncError("network", "重试耗尽")
