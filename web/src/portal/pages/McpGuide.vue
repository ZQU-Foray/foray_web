<script setup lang="ts">
/* MCP 接入引导：把 docs/portal-mcp.md 里成员自己就能做完的几步搬到页面上。
 * 只展示说明与复制用文本，不发起任何请求——令牌明文只在签发那一刻出现，
 * 这里不读取、不保存、也不回显任何令牌。 */
import PortalPage from "../components/PortalPage.vue";
import { navNo } from "../nav";

/* 与当前访问来源保持一致：内网、演示实例、正式域名都能直接照抄 */
const origin = location.origin;
const endpoint = origin + "/mcp";
const loginCmd = "python scripts/mcp_login.py --base-url " + origin;

/* 支持 streamable HTTP 的客户端直接配 URL + 头；只支持 stdio 的走 mcp-remote 中转 */
const httpConfig = JSON.stringify(
  { mcpServers: { "foray-portal": { type: "http", url: endpoint, headers: { Authorization: "Bearer <AGENT_TOKEN>" } } } },
  null, 2);
const stdioConfig = JSON.stringify(
  { mcpServers: { "foray-portal": { command: "npx",
    args: ["-y", "mcp-remote", endpoint, "--header", "Authorization: Bearer ${FORAY_AGENT_TOKEN}"] } } },
  null, 2);
const maintainerCmd = "curl -sS -X POST " + origin + "/api/portal/agents \\\n"
  + "  -H 'Content-Type: application/json' \\\n"
  + "  -b 'dsh_session=<维护者会话>' \\\n"
  + "  -d '{\"name\":\"claude-desktop\",\"open_id\":\"ou_xxxxxxxx\",\"scopes\":[\"meta.read\",\"tasks.read\"]}'";

const SCOPES: Array<[string, string, string]> = [
  ["meta.read", "分组、人员、采购选项", "是"],
  ["projects.read", "项目与里程碑", "是"],
  ["tasks.read", "事项列表与详情", "是"],
  ["schedule.read", "日程", "是"],
  ["procurement.read", "采购记录", "是"],
  ["quality.read", "数据质量报告", "是"],
  ["attendance.read", "本人考勤状态", "是"],
  ["sync.read", "同步耗时、队列积压、失败与冲突数", "否"],
];
const TOOLS: Array<[string, string]> = [
  ["portal_meta", "meta.read"], ["portal_overview", "meta.read"],
  ["portal_list_projects", "projects.read"], ["portal_get_project", "projects.read"],
  ["portal_list_tasks", "tasks.read"], ["portal_get_task", "tasks.read"],
  ["portal_list_schedule", "schedule.read"],
  ["portal_list_procurement", "procurement.read"], ["portal_get_procurement", "procurement.read"],
  ["portal_quality", "quality.read"],
  ["portal_my_attendance", "attendance.read"],
  ["portal_sync_status", "sync.read"],
];
const TROUBLE: Array<[string, string]> = [
  ["401 令牌无效或已撤销", "令牌写错或已撤销，重新签发；旧令牌不再可用"],
  ["403 浏览器来源不允许", "客户端带了 Origin 头，请用 CLI/agent 客户端，别用网页直接调"],
  ["-32001 scope 不足", "撤销后按需要的 scope 重新签发"],
  ["-32010 尚无快照", "等一轮同步完成（总览页的同步状态会显示）"],
  ["expired_token", "设备码 10 分钟过期或已被换过一次，重新运行上面的命令"],
  ["确认页 403 组织不匹配", "登录账号不在本站允许的组织里，换账号或请维护者代发"],
  ["确认页 409 令牌数量已达上限", "每人最多 5 个未撤销令牌，先撤销不用的"],
];

</script>

<template>
  <PortalPage :no="navNo('/mcp-guide')" en="DIAGNOSTICS │ MCP" title="MCP 接入"
              desc="只读数据接口 │ 设备码授权 │ 配一次就能让 agent 读战队数据" watermark="MCP">
    <section class="module enter">
      <div class="module-head">
        <span class="module-tag">ENDPOINT / 01</span><h2>端点与凭据</h2>
        <span class="module-sub">只读：写入、烧录、部署、打卡一律不开放</span>
      </div>
      <div class="module-body">
        <div class="tbl-wrap">
          <table class="tbl">
            <thead><tr><th>项目</th><th>值</th></tr></thead>
            <tbody>
              <tr><td data-label="项目">端点</td><td data-label="值" class="t-mono">{{ endpoint }}</td></tr>
              <tr><td data-label="项目">协议</td><td data-label="值">JSON-RPC 2.0，streamable HTTP，单条消息</td></tr>
              <tr><td data-label="项目">鉴权</td><td data-label="值" class="t-mono">Authorization: Bearer agent_xxxxx</td></tr>
              <tr><td data-label="项目">不能用</td><td data-label="值">浏览器会话（dsh_session）——它绑的是登录状态，无法单独撤销</td></tr>
            </tbody>
          </table>
        </div>
        <p id="mcp-why" class="note">agent 用独立令牌：可单独撤销、按数据域收窄、每次调用进审计；
          飞书的 access token 既不落盘也不会发给客户端。</p>
      </div>
    </section>

    <section id="mcp-token" class="module enter">
      <div class="module-head">
        <span class="module-tag">TOKEN / 02</span><h2>拿令牌</h2>
        <span class="module-sub">成员自助最快；维护者也可以代发</span>
      </div>
      <div class="module-body">
        <h3 class="mcp-h">方式一 · 成员自助（设备码授权，推荐）</h3>
        <p class="mcp-p">命令行申请设备码 → 浏览器用飞书登录确认 → 命令行换到令牌。
          令牌存到 <span class="t-mono">~/.config/foray-mcp/token.json</span>（权限 600）。</p>
        <pre class="mcp-code t-mono">{{ loginCmd }}</pre>
        <p class="mcp-p">只想打印不落盘加 <span class="t-mono">--no-save</span>；
          想改客户端名字（会显示在确认页与令牌列表）加 <span class="t-mono">--client-name cursor</span>。</p>

        <h3 class="mcp-h">方式二 · 维护者代发</h3>
        <p class="mcp-p">维护者 = 应急账号会话，或 open_id 在 <span class="t-mono">PORTAL_MAINTAINER_IDS</span> 里。
          返回的 <span class="t-mono">token</span> 只出现这一次，记得当场存好。</p>
        <pre class="mcp-code t-mono">{{ maintainerCmd }}</pre>
        <p class="mcp-p">列出与撤销：<span class="t-mono">GET {{ origin }}/api/portal/agents</span>、
          <span class="t-mono">POST {{ origin }}/api/portal/agents/&lt;id&gt;/revoke</span>。
          撤销后下一次请求即 401，不用重启服务。</p>
      </div>
    </section>

    <section id="mcp-client" class="module enter">
      <div class="module-head">
        <span class="module-tag">CLIENT / 03</span><h2>客户端配置</h2>
        <span class="module-sub">支持 streamable HTTP 的直接填 URL；只支持 stdio 的用 mcp-remote 中转</span>
      </div>
      <div class="module-body">
        <h3 class="mcp-h">支持 streamable HTTP（Claude Code、Cursor 等）</h3>
        <pre class="mcp-code t-mono">{{ httpConfig }}</pre>
        <h3 class="mcp-h">只支持 stdio（Claude Desktop 等）</h3>
        <pre class="mcp-code t-mono">{{ stdioConfig }}</pre>
        <p class="mcp-p">把 <span class="t-mono">&lt;AGENT_TOKEN&gt;</span> 换成上一步拿到的令牌；
          mcp-remote 那版可用环境变量 <span class="t-mono">FORAY_AGENT_TOKEN</span>，避免把令牌写进配置文件。</p>
      </div>
    </section>

    <section id="mcp-scopes" class="module enter">
      <div class="module-head">
        <span class="module-tag">SCOPE / 04</span><h2>能读什么</h2>
        <span class="module-sub">未授权的工具在 tools/list 里根本不出现，直接调用返回 -32001</span>
      </div>
      <div class="module-body">
        <div class="tbl-wrap">
          <table class="tbl cards-sm">
            <thead><tr><th>scope</th><th>内容</th><th>自助签发是否默认</th></tr></thead>
            <tbody>
              <tr v-for="s in SCOPES" :key="s[0]">
                <td data-label="scope" class="t-mono">{{ s[0] }}</td>
                <td data-label="内容">{{ s[1] }}</td>
                <td data-label="默认">{{ s[2] }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="mcp-p"><span class="t-mono">attendance.read</span> 只返回令牌绑定成员自己的考勤，不提供他人明细。</p>
        <div class="tbl-wrap">
          <table class="tbl cards-sm">
            <thead><tr><th>工具</th><th>scope</th></tr></thead>
            <tbody>
              <tr v-for="t in TOOLS" :key="t[0]">
                <td data-label="工具" class="t-mono">{{ t[0] }}</td>
                <td data-label="scope" class="t-mono">{{ t[1] }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="mcp-p">分页统一 <span class="t-mono">limit</span>（上限 200）/ <span class="t-mono">offset</span>；
          返回体外层带 <span class="t-mono">snapshot_version</span> / <span class="t-mono">last_success_at</span> /
          <span class="t-mono">stale</span>，方便 agent 判断数据新旧。</p>
      </div>
    </section>

    <section id="mcp-trouble" class="module enter">
      <div class="module-head">
        <span class="module-tag">TROUBLE / 05</span><h2>连不上时</h2>
        <span class="module-sub">探活请用 POST 的 ping；GET /mcp 返回 405（不提供 SSE 流）</span>
      </div>
      <div class="module-body">
        <div class="tbl-wrap">
          <table class="tbl cards-sm">
            <thead><tr><th>现象</th><th>处理</th></tr></thead>
            <tbody>
              <tr v-for="t in TROUBLE" :key="t[0]">
                <td data-label="现象" class="t-mono">{{ t[0] }}</td>
                <td data-label="处理">{{ t[1] }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="note">完整说明（限频、审计字段、协议细节）见仓库里的
          <span class="t-mono">docs/portal-mcp.md</span>。</p>
      </div>
    </section>
  </PortalPage>
</template>

<style scoped>
.mcp-h {
  margin: 18px 0 6px;
  font-size: 13px;
  font-weight: 600;
  color: var(--c-ink);
}
.mcp-h:first-child { margin-top: 0; }
.mcp-p {
  margin: 6px 0 10px;
  font-size: 13px;
  line-height: 1.75;
  color: var(--c-text-2);
}
.mcp-code {
  margin: 0 0 10px;
  padding: 12px 14px;
  overflow-x: auto;
  font-size: 12px;
  line-height: 1.7;
  color: var(--c-ink-2);
  background: var(--c-surface);
  border: 1px solid var(--c-divider);
  white-space: pre;
}
</style>
