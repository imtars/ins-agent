<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
const access = ref(sessionStorage.getItem('access') || '')
const refresh = ref(sessionStorage.getItem('refresh') || '')
const role = ref(sessionStorage.getItem('role') || '')
const username = ref(sessionStorage.getItem('username') || '')
const loginName = ref(''), loginPassword = ref(''), page = ref('workbench'), error = ref(''), query = ref('')
const runs = ref([]), runId = ref(''), run = ref(null), artifacts = ref({}), events = ref([]), report = ref(null), evaluation = ref({})
const decision = ref('approve'), targets = ref([]), comment = ref('')
let timer
let streamAbort
async function followEvents() {
  streamAbort?.abort()
  if (!access.value || !runId.value || page.value !== 'detail') return
  streamAbort = new AbortController()
  try {
    const after = events.value.at(-1)?.id || 0
    const response = await fetch(`/api/runs/${runId.value}/stream?after_id=${after}`, {
      headers: { Authorization: `Bearer ${access.value}` }, signal: streamAbort.signal
    })
    if (!response.ok || !response.body) return
    const reader = response.body.getReader(), decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let boundary
      while ((boundary = buffer.indexOf('\n\n')) >= 0) {
        const block = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2)
        const line = block.split('\n').find(x => x.startsWith('data: '))
        if (!line) continue
        const item = JSON.parse(line.slice(6))
        if (!events.value.some(x => x.id === item.id)) events.value.push(item)
        if (['workflow.interrupted','workflow.completed','workflow.failed'].includes(item.event_type)) loadRun()
      }
    }
  } catch (e) { if (e.name !== 'AbortError') error.value = e.message }
}
function saveTokens(t) { for (const [k,v] of Object.entries({access:t.access_token,refresh:t.refresh_token,role:t.role,username:t.username})) { sessionStorage.setItem(k,v); ({access,refresh,role,username})[k].value=v } }
async function api(path, options={}) {
  const headers={...options.headers}; if(access.value) headers.Authorization=`Bearer ${access.value}`
  if(options.body) headers['Content-Type']='application/json'
  let response=await fetch(`/api${path}`,{...options,headers})
  if(response.status===401 && refresh.value && !path.startsWith('/auth/')) {
    const renewed=await fetch('/api/auth/refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({refresh_token:refresh.value})})
    if(renewed.ok) { saveTokens(await renewed.json()); headers.Authorization=`Bearer ${access.value}`; response=await fetch(`/api${path}`,{...options,headers}) }
  }
  if(!response.ok) { const body=await response.json().catch(()=>({})); throw new Error(body.detail || `${response.status} ${response.statusText}`) }
  return response.json()
}
async function login() { try { error.value=''; saveTokens(await api('/auth/login',{method:'POST',body:JSON.stringify({username:loginName.value,password:loginPassword.value})})); loginPassword.value=''; await loadRuns() } catch(e) { error.value=e.message } }
function logout() { sessionStorage.clear(); access.value='';refresh.value='';role.value='';username.value='';run.value=null;runs.value=[] }
async function loadRuns(status) { try { runs.value=(await api(`/runs${status?`?status=${status}`:''}`)).runs } catch(e) { error.value=e.message } }
async function createRun() { try { error.value=''; const r=await api('/runs',{method:'POST',body:JSON.stringify({query:query.value})}); runId.value=r.run_id;page.value='detail';await loadRun();await loadRuns() } catch(e) { error.value=e.message } }
async function loadRun() { if(!runId.value)return; try { const [s,e,a]=await Promise.all([api(`/runs/${runId.value}`),api(`/runs/${runId.value}/events`),api(`/runs/${runId.value}/artifacts`)]);run.value=s;events.value=e.events;artifacts.value=a.artifacts;report.value=s.refs.analysis?await api(`/runs/${runId.value}/report`):null } catch(e) { error.value=e.message } }
function openRun(id) { runId.value=id;page.value='detail';loadRun() }
async function submitReview() { const a=run.value.refs.analysis;try { await api(`/runs/${runId.value}/review`,{method:'POST',body:JSON.stringify({decision:decision.value,artifact_id:a.artifact_id,artifact_version:a.version,content_hash:a.content_hash,rerun_targets:decision.value==='reject'?targets.value:[],comment:comment.value})});await loadRun();await loadRuns('WAITING_APPROVAL') } catch(e){error.value=e.message} }
async function loadEvaluation(kind) { try { evaluation.value=await api(`/evaluations/${kind}`) } catch(e){error.value=e.message} }
const canReview=computed(()=>['reviewer','admin'].includes(role.value))
const trace=computed(()=>new Set((run.value?.trace||[]).map(x=>x==='data_analyst'?'sql':x==='knowledge_researcher'?'rag':x==='verifier'?'verification':x)))
watch(page,p=>{if(p==='review')loadRuns('WAITING_APPROVAL');if(p==='evaluation')loadEvaluation('rag')})
watch([runId,page,access], followEvents)
onMounted(()=>{if(access.value)loadRuns();timer=setInterval(()=>{if(access.value&&runId.value&&page.value==='detail')loadRun();if(access.value&&page.value==='review')loadRuns('WAITING_APPROVAL')},2000)})
onUnmounted(()=>{clearInterval(timer);streamAbort?.abort()})
</script>
<template>
<div class="shell"><header><div><strong>INS / AGENT</strong><span>保险分析工作台 · synthetic 演示</span></div><div v-if="access">{{ username }} · {{ role }} <button class="ghost" @click="logout">退出</button></div></header>
<main v-if="!access" class="login card"><p class="eyebrow">SECURE ACCESS</p><h1>进入工作台</h1><label>账号<input v-model="loginName" autocomplete="username"></label><label>密码<input v-model="loginPassword" type="password" autocomplete="current-password" @keyup.enter="login"></label><button @click="login">登录</button><p v-if="error" class="error">{{ error }}</p></main>
<template v-else><nav><button v-for="item in [['workbench','工作台'],['detail','运行详情'],['review','审核队列'],['evaluation','评测']]" :key="item[0]" :class="{active:page===item[0]}" @click="page=item[0]">{{ item[1] }}</button></nav><main>
<section v-if="page==='workbench'"><p class="eyebrow">WORKBENCH</p><h1>提出保险运营或条款问题</h1><div class="card"><label>分析问题<textarea v-model="query" rows="4" placeholder="例如：统计某产品的赔付率，并对照对应 synthetic 条款解释。"></textarea></label><button :disabled="!query.trim()" @click="createRun">创建运行</button></div><h2>最近运行</h2><div class="list"><button v-for="r in runs" :key="r.run_id" class="run-row" @click="openRun(r.run_id)"><span>{{ r.run_id.slice(0,8) }}</span><span>{{ r.status }}</span><span>{{ r.updated_at }}</span></button></div></section>
<section v-if="page==='detail'"><p class="eyebrow">RUN DETAIL</p><h1>运行详情</h1><div class="card"><label>Run ID<input v-model="runId" @keyup.enter="loadRun"></label><button @click="loadRun">读取</button></div><template v-if="run"><div class="card summary"><strong>{{ run.status }}</strong><span>worker attempt {{ run.job.attempt }} · cycle {{ run.cycle||'—' }}</span><span v-if="run.degraded_flags.length">降级：{{ run.degraded_flags.join(', ') }}</span></div><div class="steps"><div v-for="node in ['planner','sql','rag','synthesis','verification','human_review']" :key="node" :class="{done:trace.has(node)}">{{ trace.has(node)?'✓':'○' }} {{ node }}</div></div><div class="grid"><div class="card"><h2>事件与重试</h2><div v-for="e in events" :key="e.id" class="event"><small>{{ e.created_at }} · attempt {{ e.attempt }}</small><b>{{ e.event_type }}</b><pre v-if="e.event_type.includes('retry')||e.event_type.includes('failed')">{{ JSON.stringify(e.payload,null,2) }}</pre></div></div><div class="card"><h2>Agent artifacts / SQL / evidence</h2><details v-for="(a,stage) in artifacts" :key="stage"><summary>{{ stage }} · v{{ a.ref.version }} · {{ a.ref.content_hash.slice(0,12) }}</summary><pre>{{ JSON.stringify(a.content,null,2) }}</pre></details></div></div><div v-if="report" class="card"><h2>报告 · {{ report.published?'已发布':'审核前预览' }}</h2><p>{{ report.analysis.summary }}</p><p v-for="c in report.analysis.claims" :key="c.text">{{ c.text }} <small>{{ c.source_ids.join(', ') }}</small></p></div></template></section>
<section v-if="page==='review'"><p class="eyebrow">HUMAN REVIEW</p><h1>待审核运行</h1><p v-if="!canReview">当前账号只能查看，不能审核。</p><div class="list"><button v-for="r in runs" :key="r.run_id" class="run-row" @click="runId=r.run_id;loadRun()"><span>{{ r.run_id.slice(0,8) }}</span><span>{{ r.status }}</span></button></div><div v-if="canReview&&run?.status==='WAITING_APPROVAL'" class="card"><h2>审核当前报告</h2><p>{{ report?.analysis.summary }}</p><p>Analysis v{{ run.refs.analysis.version }} · {{ run.refs.analysis.content_hash }}</p><p v-for="issue in artifacts.verification?.content.issues" :key="issue">问题：{{ issue }}</p><label>决定<select v-model="decision"><option value="approve">批准</option><option value="reject">拒绝</option></select></label><div v-if="decision==='reject'">重跑节点：<label v-for="t in ['sql','rag','synthesis']" :key="t"><input v-model="targets" type="checkbox" :value="t">{{ t }}</label></div><label>备注<textarea v-model="comment"></textarea></label><button @click="submitReview">提交审核</button></div></section>
<section v-if="page==='evaluation'"><p class="eyebrow">ACCEPTED REPORTS</p><h1>历史评测</h1><div class="tabs"><button @click="loadEvaluation('rag')">RAG</button><button @click="loadEvaluation('sql')">SQL</button></div><div v-if="evaluation.id" class="card"><h2>{{ evaluation.id.toUpperCase() }} · {{ evaluation.status }}</h2><p>报告 SHA-256：<code>{{ evaluation.sha256 }}</code></p><div class="metric-grid"><div v-for="(v,k) in evaluation.metrics" :key="k" class="metric"><small>{{ k }}</small><strong>{{ typeof v==='number'?v.toFixed(4):JSON.stringify(v) }}</strong></div></div><p class="muted">来自固定历史报告；本页面未重新运行评测。</p></div></section>
<p v-if="error" class="error" role="alert">{{ error }}</p></main></template><footer>仅供 synthetic 保险流程演示。公开示范条款保持 draft 来源标识。</footer></div>
</template>
