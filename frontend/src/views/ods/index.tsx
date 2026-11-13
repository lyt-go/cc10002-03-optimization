import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/ods'
const columns = [
  "表编号",
  "表名称",
  "来源系统",
  "原始字段数",
  "增量字段",
  "全量快照策略",
  "每日增量行",
  "最近加载时间"
]
// 审批凭证按这四项配置签发，任何一项变化都会让旧结论立即失效。
const fingerprintFields = ["来源系统", "增量字段", "全量快照策略", "最近加载时间"]
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
type Credential = Record<string, any>
type Diagnostic = Record<string, any>
type ApprovalDetail = {
  准入状态?: string
  可用?: boolean
  不可用原因?: string
  配置指纹?: string
  当前配置摘要?: string
  凭证?: Credential | null
  审批凭证?: Credential | null
  诊断记录?: Diagnostic[]
  判定时间?: string
}

export default function OdsView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [noticeMessage, setNoticeMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [detail, setDetail] = useState<{ row: Row; approval: ApprovalDetail; credentials: Credential[] } | null>(null)

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('贴源表列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '贴源层列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function openApproval(row: Row) {
    setErrorMessage('')
    try {
      const approvalResponse = await request(`${ENDPOINT}/${row.id}/approval`)
      if (!approvalResponse.ok) {
        throw new Error('贴源审批详情读取失败')
      }
      const approval = (await approvalResponse.json()) as ApprovalDetail
      const credentialResponse = await request(`${ENDPOINT}/${row.id}/credentials`)
      const history = credentialResponse.ok ? await credentialResponse.json() : { items: [] }
      setDetail({ row, approval, credentials: history.items ?? [] })
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '贴源审批详情读取失败')
    }
  }

  async function review(row: Row) {
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/review`, {
        method: 'POST',
        body: JSON.stringify({ values: { action: '提交建表审批' } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || '审批结论未更新')
      }
      setNoticeMessage(payload.message || '审批结论已更新')
      await reload()
      await openApproval(row)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '贴源审批失败')
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
        throw new Error(payload.message || '贴源层动作未生效')
      }
      setNoticeMessage(payload.message || '')
      await reload()
      await openApproval(row)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '贴源层操作失败')
    }
  }

  const stats = useMemo(() => {
    const online = rows.filter((row) => row.status === '已上线')
    const approving = rows.filter((row) => row.status === '建表审批中')
    const reusable = online.filter((row) => row.审批结论 === '可准入')
    return [
      { label: '贴源表总数', value: total },
      { label: '已上线表', value: online.length },
      { label: '建表审批中', value: approving.length },
      { label: '结论可复用', value: reusable.length },
    ]
  }, [rows, total])

  return (
    <section className="page" data-module="ods">
      <header className="page-head">
        <div>
          <h2>贴源层管理</h2>
          <p className="page-desc">管理贴源层（ODS）数据表，按来源系统、增量字段、快照策略和最近加载时间签发准入凭证。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setNoticeMessage('请先在明细层确认上游贴源表，再发起建表审批')}>
            登记贴源表
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出贴源层清单
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
            <th>审批结论</th>
            <th>凭证有效期至</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>
                <span className={`badge ${row.审批结论 === '可准入' ? 'ok' : 'warn'}`}>{row.审批结论 ?? '—'}</span>
                {row.不可用原因 && <small className="cell-note">{row.不可用原因}</small>}
              </td>
              <td>{row.凭证有效期至 ?? '—'}</td>
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void openApproval(row)}>审批详情</button>
                <button className="link" type="button" onClick={() => void review(row)}>申请凭证</button>
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
              <td className="empty-state" colSpan={columns.length + 3}>暂无贴源层数据，可先登记贴源表</td>
            </tr>
          )}
        </tbody>
      </table>

      {detail && (
        <aside className="detail-panel">
          <header className="detail-head">
            <div>
              <h3>贴源审批详情 · {detail.row.表编号}</h3>
              <p className="page-desc">审批详情与明细层上游准入读取的是同一份凭证结论。</p>
            </div>
            <button className="btn ghost" type="button" onClick={() => setDetail(null)}>关闭</button>
          </header>

          <div className="detail-grid">
            <div><span className="stat-label">准入状态</span><strong>{detail.approval.准入状态 ?? '—'}</strong></div>
            <div><span className="stat-label">配置指纹</span><strong>{detail.approval.配置指纹 ?? '—'}</strong></div>
            <div><span className="stat-label">判定时间</span><strong>{detail.approval.判定时间 ?? '—'}</strong></div>
            <div><span className="stat-label">不可用原因</span><strong>{detail.approval.不可用原因 || '—'}</strong></div>
          </div>

          <p className="cell-note">当前配置：{detail.approval.当前配置摘要 ?? '—'}</p>

          {detail.approval.凭证 ? (
            <table className="data-table compact">
              <thead>
                <tr><th>凭证编号</th><th>凭证状态</th><th>配置指纹</th><th>签发时间</th><th>有效期至</th><th>失效原因</th></tr>
              </thead>
              <tbody>
                <tr>
                  <td>{detail.approval.凭证.凭证编号}</td>
                  <td>{detail.approval.凭证.凭证状态}</td>
                  <td>{detail.approval.凭证.配置指纹}</td>
                  <td>{detail.approval.凭证.签发时间}</td>
                  <td>{detail.approval.凭证.有效期至}</td>
                  <td>{detail.approval.凭证.失效原因 || '—'}</td>
                </tr>
              </tbody>
            </table>
          ) : (
            <p className="empty-state">尚未签发准入凭证</p>
          )}

          <h4>凭证台账（按落库顺序）</h4>
          <table className="data-table compact">
            <thead>
              <tr><th>凭证编号</th><th>审核来源</th><th>状态</th><th>签发时间</th><th>失效原因</th></tr>
            </thead>
            <tbody>
              {detail.credentials.map((item) => (
                <tr key={String(item.id)}>
                  <td>{item.凭证编号}</td>
                  <td>{item.operator}</td>
                  <td>{item.凭证状态}</td>
                  <td>{item.签发时间}</td>
                  <td>{item.失效原因 || '—'}</td>
                </tr>
              ))}
              {detail.credentials.length === 0 && (
                <tr><td className="empty-state" colSpan={5}>暂无凭证记录</td></tr>
              )}
            </tbody>
          </table>

          <h4>诊断记录（周期内仅保留一条）</h4>
          <table className="data-table compact">
            <thead>
              <tr><th>诊断编号</th><th>诊断结论</th><th>配置指纹</th><th>复用次数</th><th>诊断周期结束</th></tr>
            </thead>
            <tbody>
              {(detail.approval.诊断记录 ?? []).map((item) => (
                <tr key={String(item.id)}>
                  <td>{item.诊断编号}</td>
                  <td>{item.诊断结论}</td>
                  <td>{item.配置指纹}</td>
                  <td>{item.复用次数}</td>
                  <td>{item.诊断周期结束}</td>
                </tr>
              ))}
              {(detail.approval.诊断记录 ?? []).length === 0 && (
                <tr><td className="empty-state" colSpan={5}>暂无诊断记录</td></tr>
              )}
            </tbody>
          </table>

          <p className="cell-note">凭证按「{fingerprintFields.join('、')}」签发；任一字段变化会让旧结论立刻失效。</p>
        </aside>
      )}

      <footer className="page-foot">
        <span>共 {total} 条贴源层记录</span>
        {noticeMessage && <span className="notice-text">{noticeMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
