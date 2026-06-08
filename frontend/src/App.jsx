import { useEffect, useRef, useState } from 'react'
import './App.css'

const API_BASE = 'http://localhost:8000'

// noVNC web client served by the selenium image (password "secret").
const vncUrl = (port) =>
  `http://localhost:${port}/?autoconnect=1&resize=scale&password=secret`

const SUB_TABS = ['Live view', 'Logs', 'Output']

const STATUS_META = {
  idle: { color: 'var(--text-dim)', label: 'idle' },
  building: { color: 'var(--amber)', label: 'building' },
  done: { color: 'var(--green)', label: 'ready' },
  error: { color: 'var(--red)', label: 'error' },
}

export default function App() {
  // upload form (lives in the "+ New code" tab)
  const [scraperFile, setScraperFile] = useState(null)
  const [requirementsFile, setRequirementsFile] = useState(null)
  const [dataFile, setDataFile] = useState(null)
  const [codeName, setCodeName] = useState('')

  // build stream
  const [buildLog, setBuildLog] = useState('')
  const [buildStatus, setBuildStatus] = useState('idle')
  const [buildLogsByName, setBuildLogsByName] = useState({}) // name -> build log

  // containers + navigation
  const [containers, setContainers] = useState([])
  const [activeTab, setActiveTab] = useState('new') // 'new' | container name
  const [subTab, setSubTab] = useState('Live view')
  const [output, setOutput] = useState('') // active container's docker logs
  const [vncKey, setVncKey] = useState(0)

  const buildLogRef = useRef(null)
  const outputRef = useRef(null)

  const uploads = [
    { field: 'scraper', label: 'scraper.py', accept: '.py', icon: '📄',
      value: scraperFile, set: setScraperFile },
    { field: 'requirements', label: 'requirements.txt', accept: '.txt', icon: '📄',
      value: requirementsFile, set: setRequirementsFile },
    { field: 'data', label: 'data file', accept: '.csv,.xlsx,.xls', icon: '📊',
      value: dataFile, set: setDataFile, hint: 'csv / excel · optional' },
  ]

  const activeContainer = containers.find((c) => c.name === activeTab) || null

  const scrollToBottom = (ref) => {
    requestAnimationFrame(() => {
      if (ref.current) ref.current.scrollTop = ref.current.scrollHeight
    })
  }

  const refreshContainers = async () => {
    try {
      const res = await fetch(`${API_BASE}/containers`)
      if (res.ok) setContainers(await res.json())
    } catch {
      // backend not reachable — keep current list
    }
  }

  // ----- poll the container list every 3s -----
  useEffect(() => {
    let active = true
    const tick = async () => {
      try {
        const res = await fetch(`${API_BASE}/containers`)
        if (active && res.ok) setContainers(await res.json())
      } catch {
        // ignore
      }
    }
    tick()
    const id = setInterval(tick, 3000)
    return () => {
      active = false
      clearInterval(id)
    }
  }, [])

  // ----- stream the ACTIVE container's logs into its Output sub-tab -----
  useEffect(() => {
    if (activeTab === 'new') return
    const controller = new AbortController()

    const streamOutput = async () => {
      try {
        const res = await fetch(`${API_BASE}/scraper-output/${activeTab}`, {
          signal: controller.signal,
        })
        if (!res.ok || !res.body) return
        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          setOutput((prev) => prev + decoder.decode(value, { stream: true }))
          scrollToBottom(outputRef)
        }
      } catch {
        // aborted on tab change/unmount — ignore
      }
    }

    streamOutput()
    return () => controller.abort()
  }, [activeTab])

  const goToCode = (name) => {
    setOutput('')
    setActiveTab(name)
    setSubTab('Live view')
    setVncKey((k) => k + 1)
  }

  const stopContainer = async (name, e) => {
    e.stopPropagation()
    try {
      await fetch(`${API_BASE}/containers/${name}/stop`, { method: 'POST' })
    } catch {
      // ignore — refresh reflects reality
    }
    if (activeTab === name) setActiveTab('new')
    refreshContainers()
  }

  const handleBuild = async () => {
    if (!scraperFile || !requirementsFile) {
      setBuildLog('Please upload both scraper.py and requirements.txt first.\n')
      return
    }

    setBuildStatus('building')
    setBuildLog('')

    const form = new FormData()
    form.append('scraper', scraperFile)
    form.append('requirements', requirementsFile)
    if (dataFile) form.append('data', dataFile)
    if (codeName.trim()) form.append('name', codeName.trim())

    let acc = ''
    let buildName = null

    try {
      const res = await fetch(`${API_BASE}/scrapers-upload`, {
        method: 'POST',
        body: form,
      })
      if (!res.ok || !res.body) {
        setBuildLog(`Request failed: ${res.status} ${res.statusText}\n`)
        setBuildStatus('error')
        return
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let failed = false

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        const text = decoder.decode(value, { stream: true })
        acc += text
        setBuildLog(acc)
        scrollToBottom(buildLogRef)

        if (text.includes('[error]')) failed = true

        const slot = text.match(/\[slot\] container=(\S+)/)
        if (slot) buildName = slot[1]

        const ready = text.match(/\[vnc-ready\] name=(\S+) port=(\d+)/)
        if (ready) {
          refreshContainers()
          goToCode(ready[1]) // auto-focus the code we just launched
        }
      }

      if (buildName) {
        const finalLog = acc
        setBuildLogsByName((prev) => ({ ...prev, [buildName]: finalLog }))
      }
      setBuildStatus(failed ? 'error' : 'done')
      refreshContainers()
      if (!failed) {
        setScraperFile(null)
        setRequirementsFile(null)
        setDataFile(null)
        setCodeName('')
      }
    } catch (err) {
      setBuildLog(acc + `\nConnection error: ${err.message}\n`)
      setBuildStatus('error')
    }
  }

  const meta = STATUS_META[buildStatus]
  const missingFiles = [
    !scraperFile && 'scraper.py',
    !requirementsFile && 'requirements.txt',
  ].filter(Boolean)

  return (
    <div className="shell">
      {/* ---------- App bar ---------- */}
      <header className="appbar">
        <div className="brand">
          <div className="brand-logo">🕸️</div>
          <div>
            <div className="brand-title">Scraper Orchestrator</div>
            <div className="brand-sub">build · run · watch live</div>
          </div>
        </div>
        <div className="status" style={{ color: meta.color }}>
          <span className={`status-dot${buildStatus === 'building' ? ' pulse' : ''}`} />
          <span style={{ color: 'var(--text)' }}>{meta.label}</span>
        </div>
      </header>

      {/* ---------- Code tabs ---------- */}
      <div className="code-tabs">
        {containers.map((c) => {
          const running = c.status === 'running'
          return (
            <button
              key={c.name}
              className={`code-tab${activeTab === c.name ? ' active' : ''}`}
              onClick={() => goToCode(c.name)}
            >
              <span
                className="code-tab-dot"
                style={{ color: running ? 'var(--green)' : 'var(--text-faint)' }}
              />
              <span className="code-tab-label">{c.label || c.name}</span>
              <span
                className="code-tab-x"
                title="stop & remove"
                onClick={(e) => stopContainer(c.name, e)}
              >
                ✕
              </span>
            </button>
          )
        })}
        <button
          className={`newcode-tab${activeTab === 'new' ? ' active' : ''}`}
          onClick={() => setActiveTab('new')}
        >
          ＋ New code
        </button>
      </div>

      {/* ---------- Stage ---------- */}
      <div className="stage">
        {activeTab === 'new' ? (
          <NewCodePanel
            uploads={uploads}
            codeName={codeName}
            setCodeName={setCodeName}
            onBuild={handleBuild}
            building={buildStatus === 'building'}
            missingFiles={missingFiles}
            buildLog={buildLog}
            buildLogRef={buildLogRef}
          />
        ) : activeContainer ? (
          <CodePanel
            container={activeContainer}
            subTab={subTab}
            setSubTab={setSubTab}
            vncKey={vncKey}
            reconnect={() => setVncKey((k) => k + 1)}
            buildLog={buildLogsByName[activeContainer.name]}
            output={output}
            outputRef={outputRef}
            onStop={(e) => stopContainer(activeContainer.name, e)}
          />
        ) : (
          <div className="placeholder">
            <div className="placeholder-icon">🗑️</div>
            <div className="placeholder-text">this container no longer exists</div>
          </div>
        )}
      </div>
    </div>
  )
}

// ---------- "+ New code" panel ----------
function NewCodePanel({
  uploads, codeName, setCodeName, onBuild, building, missingFiles,
  buildLog, buildLogRef,
}) {
  return (
    <div className="newcode">
      <div className="newcode-form">
        <div className="section-title">Files</div>
        <div className="upload-row">
          {uploads.map((u) => {
            const uploaded = u.value
            return (
              <label key={u.field} className={`upload-card${uploaded ? ' filled' : ''}`}>
                <input
                  type="file"
                  accept={u.accept}
                  style={{ display: 'none' }}
                  onChange={(e) => u.set(e.target.files[0] || null)}
                />
                <div className="upload-icon">{uploaded ? '✅' : u.icon}</div>
                <div className="upload-name">{u.label}</div>
                <div className="upload-hint">
                  {uploaded ? uploaded.name : u.hint || 'click to upload'}
                </div>
              </label>
            )
          })}
        </div>

        <div className="run-row">
          <input
            className="name-input"
            type="text"
            placeholder="code name (optional, e.g. japan-scraper)"
            value={codeName}
            onChange={(e) => setCodeName(e.target.value)}
          />
          <button className="build-btn" onClick={onBuild} disabled={building}>
            {building ? 'Building…' : '▶  Build & run new'}
          </button>
        </div>
        <div className="hint-row">
          {missingFiles.length > 0 ? (
            <span style={{ color: 'var(--amber)' }}>
              ⬆ upload {missingFiles.join(' + ')} to build this code
            </span>
          ) : (
            'Each build launches a new container as its own tab above.'
          )}
        </div>
      </div>

      <div className="terminal newcode-log" ref={buildLogRef}>
        {buildLog || (
          <span className="terminal-empty">-- build output appears here --</span>
        )}
      </div>
    </div>
  )
}

// ---------- per-code panel (Live / Logs / Output) ----------
function CodePanel({
  container, subTab, setSubTab, vncKey, reconnect, buildLog, output, outputRef, onStop,
}) {
  const running = container.status === 'running'
  return (
    <div className="codepanel">
      <div className="panel-head">
        <div className="panel-head-left">
          <span
            className="container-dot"
            style={{ color: running ? 'var(--green)' : 'var(--text-faint)' }}
          />
          <span className="panel-title">{container.label || container.name}</span>
          <span className="panel-sub">
            {container.name} · localhost:{container.novnc_port} · {container.status}
          </span>
        </div>
        <button className="ghost-btn danger" onClick={onStop}>
          ✕ Stop
        </button>
      </div>

      <div className="subtabs">
        <div className="subtabs-left">
          {SUB_TABS.map((t) => (
            <button
              key={t}
              className={`subtab${subTab === t ? ' active' : ''}`}
              onClick={() => setSubTab(t)}
            >
              {t}
            </button>
          ))}
        </div>
        {subTab === 'Live view' && (
          <div className="vnc-bar-actions">
            <button className="ghost-btn" onClick={reconnect}>
              ↻ Reconnect
            </button>
            <a
              className="ghost-btn"
              href={vncUrl(container.novnc_port)}
              target="_blank"
              rel="noreferrer"
            >
              ↗ Open in new tab
            </a>
          </div>
        )}
      </div>

      <div className="panel-body">
        {subTab === 'Live view' ? (
          <iframe
            key={`${container.name}-${vncKey}`}
            className="vnc-frame"
            title="Live browser"
            src={vncUrl(container.novnc_port)}
          />
        ) : subTab === 'Logs' ? (
          <div className="terminal">
            {buildLog || (
              <span className="terminal-empty">
                -- build log not captured (built in an earlier session) --
              </span>
            )}
          </div>
        ) : (
          <div className="terminal output" ref={outputRef}>
            {output || (
              <span className="terminal-empty">-- waiting for scraper output --</span>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
