import { useEffect, useState } from 'react'

const STATUS_TEXT = { pending: '待处理', running: '处理中', done: '已出结论' }
const EVENT_TEXT = { start: '缓领开始', end: '缓领结束', policy: '策略更新' }

function fmtTime(value) {
  if (!value) return ''
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

export default function App() {
  const [username, setUsername] = useState('printer')
  const [password, setPassword] = useState('print123456')
  const [token, setToken] = useState(localStorage.getItem('print_token') || '')
  const [role, setRole] = useState(localStorage.getItem('print_role') || '')
  const [view, setView] = useState('queue')
  const [rows, setRows] = useState([])
  const [policy, setPolicy] = useState(null)
  const [sheet, setSheet] = useState('插页-02')
  const [cyan, setCyan] = useState('0.08')
  const [magenta, setMagenta] = useState('0.02')
  const [urgent, setUrgent] = useState(false)
  const [error, setError] = useState('')

  async function api(path, options = {}) {
    const res = await fetch(path, {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data.detail || '请求失败')
    return data
  }

  async function load() {
    const [jobs, policyData] = await Promise.all([
      api('/api/jobs'),
      api('/api/cooldown-policy'),
    ])
    setRows(jobs)
    setPolicy(policyData)
  }

  useEffect(() => {
    if (!token) return
    load()
    const timer = setInterval(load, 1000)
    return () => clearInterval(timer)
  }, [token])

  async function enter() {
    const data = await api('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    })
    localStorage.setItem('print_token', data.access_token)
    localStorage.setItem('print_role', data.role)
    setToken(data.access_token)
    setRole(data.role)
  }

  async function send() {
    setError('')
    try {
      await api('/api/jobs', {
        method: 'POST',
        body: JSON.stringify({
          sheet,
          cyan_mm: Number(cyan),
          magenta_mm: Number(magenta),
          urgent,
        }),
      })
    } catch (err) {
      setError(err.message)
    }
  }

  function leave() {
    localStorage.clear()
    setToken('')
    setRole('')
  }

  if (!token) {
    return (
      <main>
        <h1>印刷套准复核台</h1>
        <p>提交后接口只入队。另一进程领走偏差并写结论，页面轮询到结论出现。</p>
        <input value={username} onChange={(e) => setUsername(e.target.value)} />
        <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
        <button onClick={enter}>登录</button>
        <p>printer / print123456 可送复核；checker / check123456 只看</p>
      </main>
    )
  }

  return (
    <main>
      <nav className="topbar">
        <strong>印刷套准复核台</strong>
        <button className={view === 'queue' ? 'active' : ''} onClick={() => setView('queue')}>
          复核队列
        </button>
        <button className={view === 'policy' ? 'active' : ''} onClick={() => setView('policy')}>
          缓领策略
        </button>
        <button onClick={leave}>退出</button>
      </nav>
      {view === 'queue' ? (
        <QueueView
          role={role}
          rows={rows}
          policy={policy}
          sheet={sheet}
          cyan={cyan}
          magenta={magenta}
          urgent={urgent}
          error={error}
          onSheet={setSheet}
          onCyan={setCyan}
          onMagenta={setMagenta}
          onUrgent={setUrgent}
          onSend={send}
        />
      ) : (
        <PolicyView role={role} policy={policy} onSaved={load} />
      )}
    </main>
  )
}

function QueueView(props) {
  const { role, rows, policy, error } = props
  return (
    <section>
      {policy?.cooling && (
        <p className="banner">
          缓领中：最近连续套不准已达阈值 {policy.fail_threshold}，领取进程暂停领取普通任务{' '}
          {policy.remaining_seconds} 秒，急件仍可领取。
        </p>
      )}
      {role === 'writer' && (
        <p>
          <input value={props.sheet} onChange={(e) => props.onSheet(e.target.value)} />
          <input value={props.cyan} onChange={(e) => props.onCyan(e.target.value)} />
          <input value={props.magenta} onChange={(e) => props.onMagenta(e.target.value)} />
          <label>
            <input type="checkbox" checked={props.urgent} onChange={(e) => props.onUrgent(e.target.checked)} />
            急件
          </label>
          <button onClick={props.onSend}>送复核</button>
        </p>
      )}
      {error && <p>{error}</p>}
      <table>
        <thead>
          <tr><th>印张</th><th>青</th><th>品</th><th>急件</th><th>状态</th><th>结论</th></tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>
              <td>{row.sheet}</td>
              <td>{row.cyan_mm}</td>
              <td>{row.magenta_mm}</td>
              <td>{row.urgent ? '急' : ''}</td>
              <td>{STATUS_TEXT[row.status] || row.status}</td>
              <td>{row.verdict || '等待'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}

function PolicyView({ role, policy, onSaved }) {
  const [threshold, setThreshold] = useState('')
  const [seconds, setSeconds] = useState('')
  const [message, setMessage] = useState('')

  useEffect(() => {
    if (policy && threshold === '') setThreshold(String(policy.fail_threshold))
    if (policy && seconds === '') setSeconds(String(policy.cooldown_seconds))
  }, [policy])

  async function save() {
    setMessage('')
    try {
      const res = await fetch('/api/cooldown-policy', {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${localStorage.getItem('print_token')}`,
        },
        body: JSON.stringify({
          fail_threshold: Number(threshold),
          cooldown_seconds: Number(seconds),
        }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || '保存失败')
      setMessage('已保存')
      onSaved()
    } catch (err) {
      setMessage(err.message)
    }
  }

  if (!policy) return <section>加载中…</section>

  return (
    <section>
      <h2>缓领策略</h2>
      <p>
        领取进程发现最近已出结论里连续套不准达到阈值时，暂停领取普通任务若干秒，缓领期间急件仍可领取。
      </p>
      <p>
        当前状态：
        {policy.cooling
          ? `缓领中，剩余 ${policy.remaining_seconds} 秒`
          : '正常领取中'}
      </p>
      <table className="policy-form">
        <tbody>
          <tr>
            <th>连续失败阈值（套）</th>
            <td>
              <input
                value={threshold}
                disabled={role !== 'writer'}
                onChange={(e) => setThreshold(e.target.value)}
              />
            </td>
          </tr>
          <tr>
            <th>缓领秒数</th>
            <td>
              <input
                value={seconds}
                disabled={role !== 'writer'}
                onChange={(e) => setSeconds(e.target.value)}
              />
            </td>
          </tr>
        </tbody>
      </table>
      {role === 'writer' && <button onClick={save}>保存策略</button>}
      {role !== 'writer' && <p>checker 仅可查看，修改策略需印刷员登录。</p>}
      {message && <p>{message}</p>}
      <h3>缓领流水</h3>
      <table>
        <thead>
          <tr><th>时间</th><th>事件</th><th>阈值</th><th>缓领秒数</th><th>连套数</th><th>说明</th></tr>
        </thead>
        <tbody>
          {policy.events.map((event) => (
            <tr key={event.id}>
              <td>{fmtTime(event.created_at)}</td>
              <td>{EVENT_TEXT[event.kind] || event.kind}</td>
              <td>{event.fail_threshold}</td>
              <td>{event.cooldown_seconds}</td>
              <td>{event.streak}</td>
              <td>{event.note}</td>
            </tr>
          ))}
          {policy.events.length === 0 && (
            <tr><td colSpan={6}>暂无流水</td></tr>
          )}
        </tbody>
      </table>
    </section>
  )
}
