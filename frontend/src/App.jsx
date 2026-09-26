import { useEffect, useRef, useState } from 'react'

export default function App() {
  const [username, setUsername] = useState('printer')
  const [password, setPassword] = useState('print123456')
  const [token, setToken] = useState(localStorage.getItem('print_token') || '')
  const [role, setRole] = useState(localStorage.getItem('print_role') || '')
  const [view, setView] = useState('jobs')

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
    <>
      <header className="topbar">
        <strong>印刷套准复核台</strong>
        <nav>
          <button className={view === 'jobs' ? 'active' : ''} onClick={() => setView('jobs')}>
            任务队列
          </button>
          <button className={view === 'policy' ? 'active' : ''} onClick={() => setView('policy')}>
            缓领策略
          </button>
        </nav>
        <span className="spacer" />
        <button onClick={leave}>退出</button>
      </header>
      <main>{view === 'jobs' ? <JobsPage api={api} role={role} /> : <PolicyPage api={api} role={role} />}</main>
    </>
  )
}

function JobsPage({ api, role }) {
  const [rows, setRows] = useState([])
  const [throttle, setThrottle] = useState(null)
  const [sheet, setSheet] = useState('插页-02')
  const [cyan, setCyan] = useState('0.08')
  const [magenta, setMagenta] = useState('0.02')
  const [urgent, setUrgent] = useState(false)
  const [error, setError] = useState('')

  async function load() {
    setRows(await api('/api/jobs'))
    setThrottle(await api('/api/throttle/policy'))
  }

  useEffect(() => {
    load()
    const timer = setInterval(load, 1000)
    return () => clearInterval(timer)
  }, [])

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

  return (
    <>
      {throttle?.active && (
        <p className="banner">
          缓领中：连续套不准已达阈值，普通任务暂停领取，急件照常（至{' '}
          {new Date(throttle.slow_until).toLocaleTimeString()}）
        </p>
      )}
      {role === 'writer' && (
        <p>
          <input value={sheet} onChange={(e) => setSheet(e.target.value)} />
          <input value={cyan} onChange={(e) => setCyan(e.target.value)} />
          <input value={magenta} onChange={(e) => setMagenta(e.target.value)} />
          <label>
            <input type="checkbox" checked={urgent} onChange={(e) => setUrgent(e.target.checked)} />
            急件
          </label>
          <button onClick={send}>送复核</button>
        </p>
      )}
      {error && <p>{error}</p>}
      <table>
        <thead>
          <tr><th>印张</th><th>青</th><th>品</th><th>类型</th><th>状态</th><th>结论</th></tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>
              <td>{row.sheet}</td>
              <td>{row.cyan_mm}</td>
              <td>{row.magenta_mm}</td>
              <td>{row.urgent ? '急件' : '普通'}</td>
              <td>{row.status}</td>
              <td>{row.verdict || '等待'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}

function PolicyPage({ api, role }) {
  const [policy, setPolicy] = useState(null)
  const [events, setEvents] = useState([])
  const [threshold, setThreshold] = useState('')
  const [slowSeconds, setSlowSeconds] = useState('')
  const [message, setMessage] = useState('')
  const inited = useRef(false)
  const writable = role === 'writer'

  async function load() {
    const data = await api('/api/throttle/policy')
    setPolicy(data)
    if (!inited.current) {
      setThreshold(String(data.threshold))
      setSlowSeconds(String(data.slow_seconds))
      inited.current = true
    }
    setEvents(await api('/api/throttle/events'))
  }

  useEffect(() => {
    load()
    const timer = setInterval(load, 1000)
    return () => clearInterval(timer)
  }, [])

  async function save() {
    setMessage('')
    try {
      await api('/api/throttle/policy', {
        method: 'PUT',
        body: JSON.stringify({ threshold: Number(threshold), slow_seconds: Number(slowSeconds) }),
      })
      setMessage('已保存')
    } catch (err) {
      setMessage(err.message)
    }
  }

  return (
    <>
      <h2>缓领策略</h2>
      <p>
        最近结论连续
        <input
          type="number"
          min="1"
          max="20"
          value={threshold}
          disabled={!writable}
          onChange={(e) => setThreshold(e.target.value)}
        />
        笔套不准则缓领
        <input
          type="number"
          min="1"
          max="3600"
          value={slowSeconds}
          disabled={!writable}
          onChange={(e) => setSlowSeconds(e.target.value)}
        />
        秒（急件照常领取）
        {writable && <button onClick={save}>保存</button>}
      </p>
      {message && <p>{message}</p>}
      <p>
        当前状态：
        {policy?.active
          ? `缓领中，至 ${new Date(policy.slow_until).toLocaleTimeString()} 恢复普通领取`
          : '未缓领，普通任务正常领取'}
      </p>
      <h3>缓领流水</h3>
      <table>
        <thead>
          <tr><th>时间</th><th>事件</th><th>说明</th></tr>
        </thead>
        <tbody>
          {events.map((ev) => (
            <tr key={ev.id}>
              <td>{new Date(ev.created_at).toLocaleString()}</td>
              <td>{ev.event === 'start' ? '开始缓领' : '结束缓领'}</td>
              <td>{ev.detail}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}
