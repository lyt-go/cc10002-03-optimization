import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/dwd'
const columns = [
  "表编号",
  "表名称",
  "业务域",
  "主键列",
  "度量列",
  "时间分区列",
  "上游贴源表",
  "刷新水位"
]
const actions = [
  "提交建表审批",
  "确认上线",
  "下线表"
]
const statuses = [
  "草稿",
  "建表审批中",
  "已上线",
  "已下线"
]

type Row = Record<string, any>
type UpstreamOption = Record<string, any>

export default function DwdView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [options, setOptions] = useState<UpstreamOption[]>([])
  const [errorMessage, setErrorMessage] = useState('')
  const [noticeMessage, setNoticeMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const [listResponse, optionResponse] = await Promise.all([
        request(`${ENDPOINT}?${query}`),
        request(`${ENDPOINT}/upstream-options`),
      ])
      if (!listResponse.ok) {
        throw new Error('明细表列表读取失败')
      }
      const payload = await listResponse.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      if (optionResponse.ok) {
        const optionPayload = await optionResponse.json()
        setOptions(optionPayload.items ?? [])
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '明细层列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function bindUpstream(row: Row, code: string) {
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}`, {
        method: 'PUT',
        body: JSON.stringify({ values: { 上游贴源表: code } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || '上游贴源表未更新')
      }
      setNoticeMessage(`明细表 ${row.表编号} 已选择上游 ${code || '（未选择）'}`)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '上游贴源表更新失败')
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || '明细层动作未生效')
      }
      setNoticeMessage(payload.message || '')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '明细层操作失败')
    }
  }

  const stats = useMemo(() => {
    const online = rows.filter((row) => row.status === '已上线')
    const blocked = rows.filter((row) => row.上游准入状态 === '不可准入')
    return [
      { label: '明细表总数', value: total },
      { label: '已上线表', value: online.length },
      { label: '上游不可用', value: blocked.length },
    ]
  }, [rows, total])

  return (
    <section className="page" data-module="dwd">
      <header className="page-head">
        <div>
          <h2>明细层管理</h2>
          <p className="page-desc">管理明细层（DWD）数据表，上游准入直接读取贴源审批结论；贴源下线时保留历史定义并标记不可用。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setNoticeMessage('明细表登记入口尚未接入审批流')}>
            登记明细表
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出明细层清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {columns.slice(0, 3).map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>上游准入状态</th>
            <th>上游凭证</th>
            <th>不可用原因</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)} className={row.上游准入状态 === '不可准入' ? 'row-blocked' : undefined}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>
                <span className={`badge ${row.上游准入状态 === '可准入' ? 'ok' : 'warn'}`}>{row.上游准入状态 ?? '—'}</span>
                {row.受影响 && <small className="cell-note">已标记受影响</small>}
              </td>
              <td>
                {row.上游凭证编号 ?? '—'}
                {row.上游凭证有效期至 && <small className="cell-note">有效期至 {row.上游凭证有效期至}</small>}
              </td>
              <td>{row.不可用原因 || '—'}</td>
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 4}>暂无明细层数据，可先登记明细表</td>
            </tr>
          )}
        </tbody>
      </table>

      <aside className="detail-panel">
        <header className="detail-head">
          <div>
            <h3>选择上游贴源表</h3>
            <p className="page-desc">候选项直接来自贴源审批结论，与贴源表审批详情保持一致。</p>
          </div>
        </header>
        <table className="data-table compact">
          <thead>
            <tr><th>贴源表</th><th>来源系统</th><th>增量字段</th><th>快照策略</th><th>准入状态</th><th>凭证</th><th>不可用原因</th><th>绑定到明细表</th></tr>
          </thead>
          <tbody>
            {options.map((option) => (
              <tr key={String(option.table_id)}>
                <td>{option.table_code}</td>
                <td>{option.来源系统 ?? '—'}</td>
                <td>{option.增量字段 ?? '—'}</td>
                <td>{option.全量快照策略 ?? '—'}</td>
                <td>
                  <span className={`badge ${option.准入状态 === '可准入' ? 'ok' : 'warn'}`}>{option.准入状态}</span>
                </td>
                <td>{option.凭证编号 ?? '—'}</td>
                <td>{option.不可用原因 || '—'}</td>
                <td>
                  <select
                    className="select"
                    defaultValue=""
                    onChange={(event) => {
                      const row = rows.find((item) => String(item.id) === event.target.value)
                      if (row) void bindUpstream(row, option.table_code)
                    }}
                  >
                    <option value="">选择明细表</option>
                    {rows.map((row) => (
                      <option key={String(row.id)} value={String(row.id)}>{row.表编号}</option>
                    ))}
                  </select>
                </td>
              </tr>
            ))}
            {options.length === 0 && (
              <tr><td className="empty-state" colSpan={8}>暂无贴源审批结论可选用</td></tr>
            )}
          </tbody>
        </table>
      </aside>

      <footer className="page-foot">
        <span>共 {total} 条明细层记录</span>
        {noticeMessage && <span className="notice-text">{noticeMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
