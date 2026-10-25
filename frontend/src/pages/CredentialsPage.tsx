import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  App,
  Button,
  Card,
  Checkbox,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import {
  ApiOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  DeleteOutlined,
  EditOutlined,
  KeyOutlined,
  PlusOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'

import { credentialApi, providerApi } from '@/api/endpoints'
import { describeError } from '@/api/client'
import type { Credential, CredentialPayload, ProviderInfo } from '@/api/types'

const CAP_LABEL: Record<string, string> = { text: '文本', image: '图片', video: '视频' }

export default function CredentialsPage() {
  const { message } = App.useApp()
  const [providers, setProviders] = useState<ProviderInfo[]>([])
  const [items, setItems] = useState<Credential[]>([])
  const [loading, setLoading] = useState(true)
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<Credential | null>(null)
  const [saving, setSaving] = useState(false)
  const [testingId, setTestingId] = useState<number | null>(null)
  const [form] = Form.useForm<CredentialPayload>()

  const providerMap = useMemo(() => {
    const map: Record<string, ProviderInfo> = {}
    providers.forEach((p) => (map[p.key] = p))
    return map
  }, [providers])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [ps, cs] = await Promise.all([providerApi.list(), credentialApi.list()])
      setProviders(ps)
      setItems(cs)
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setLoading(false)
    }
  }, [message])

  useEffect(() => {
    void load()
  }, [load])

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue({ provider: providers[0]?.key, capabilities: providers[0]?.capabilities })
    setOpen(true)
  }

  const openEdit = (record: Credential) => {
    setEditing(record)
    form.setFieldsValue({
      provider: record.provider,
      name: record.name,
      base_url: record.base_url ?? undefined,
      capabilities: record.capabilities.split(',').filter(Boolean),
      is_default: record.is_default,
      api_key: '',
    })
    setOpen(true)
  }

  const handleSubmit = async () => {
    const values = await form.validateFields()
    setSaving(true)
    try {
      if (editing) {
        const payload: Partial<CredentialPayload> = { ...values }
        if (!payload.api_key) delete payload.api_key
        await credentialApi.update(editing.id, payload)
        message.success('密钥已更新')
      } else {
        await credentialApi.create(values)
        message.success('密钥已添加')
      }
      setOpen(false)
      await load()
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setSaving(false)
    }
  }

  const handleTest = async (id: number) => {
    setTestingId(id)
    try {
      const result = await credentialApi.test(id)
      if (result.ok) message.success(`连接成功：${result.message}`)
      else message.error(`连接失败：${result.message}`)
      await load()
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setTestingId(null)
    }
  }

  const handleDelete = async (id: number) => {
    try {
      await credentialApi.remove(id)
      message.success('密钥已删除')
      setItems((prev) => prev.filter((item) => item.id !== id))
    } catch (error) {
      message.error(describeError(error))
    }
  }

  const columns: ColumnsType<Credential> = [
    {
      title: '厂商 / 名称',
      dataIndex: 'name',
      render: (_, record) => (
        <div>
          <div style={{ fontWeight: 600 }}>
            {record.name}
            {record.is_default && (
              <Tag color="blue" style={{ marginLeft: 8 }}>
                默认
              </Tag>
            )}
          </div>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {providerMap[record.provider]?.label ?? record.provider}
          </Typography.Text>
        </div>
      ),
    },
    {
      title: 'API Key',
      dataIndex: 'masked_key',
      render: (value: string) => <Typography.Text code>{value}</Typography.Text>,
    },
    {
      title: '能力',
      dataIndex: 'capabilities',
      render: (value: string) => (
        <Space size={4} wrap>
          {value
            .split(',')
            .filter(Boolean)
            .map((cap) => (
              <Tag key={cap}>{CAP_LABEL[cap] ?? cap}</Tag>
            ))}
        </Space>
      ),
    },
    {
      title: '连通性',
      dataIndex: 'last_check_ok',
      width: 130,
      render: (ok: boolean | null, record) => {
        if (ok === null || ok === undefined) return <Typography.Text type="secondary">未测试</Typography.Text>
        return (
          <Tooltip title={record.last_check_message}>
            <span style={{ color: ok ? '#12b76a' : '#f04438' }}>
              {ok ? <CheckCircleOutlined /> : <CloseCircleOutlined />}
              <span style={{ marginLeft: 6 }}>{ok ? '可用' : '异常'}</span>
            </span>
          </Tooltip>
        )
      },
    },
    {
      title: '操作',
      key: 'actions',
      width: 210,
      render: (_, record) => (
        <Space size={4}>
          <Button
            size="small"
            icon={<ThunderboltOutlined />}
            loading={testingId === record.id}
            onClick={() => handleTest(record.id)}
          >
            测试
          </Button>
          <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(record)}>
            编辑
          </Button>
          <Popconfirm title="删除该密钥？" okText="删除" cancelText="取消" onConfirm={() => handleDelete(record.id)}>
            <Button size="small" danger type="text" icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  const selectedProvider = Form.useWatch('provider', form)
  const availableCaps = selectedProvider ? providerMap[selectedProvider]?.capabilities ?? [] : []

  return (
    <div>
      <div className="page-head">
        <div>
          <h2 className="page-title">密钥管理</h2>
          <p className="page-sub">
            添加你自己的厂商 API Key，平台用这些 Key 调用模型；密钥加密存储，仅用于本账号。
          </p>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate} disabled={providers.length === 0}>
          添加密钥
        </Button>
      </div>

      <Card styles={{ body: { padding: 0 } }}>
        <Table<Credential>
          rowKey="id"
          loading={loading}
          columns={columns}
          dataSource={items}
          pagination={false}
          locale={{
            emptyText: (
              <Empty
                image={<KeyOutlined style={{ fontSize: 42, color: '#c3c9d6' }} />}
                description="还没有配置任何密钥"
              >
                <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
                  添加密钥
                </Button>
              </Empty>
            ),
          }}
        />
      </Card>

      <Modal
        title={editing ? '编辑密钥' : '添加密钥'}
        open={open}
        onCancel={() => setOpen(false)}
        onOk={handleSubmit}
        confirmLoading={saving}
        okText="保存"
        cancelText="取消"
        destroyOnClose
      >
        <Form form={form} layout="vertical" requiredMark={false} style={{ marginTop: 12 }}>
          <Form.Item name="provider" label="厂商" rules={[{ required: true, message: '请选择厂商' }]}>
            <Select
              disabled={!!editing}
              onChange={(key) => {
                const spec = providerMap[key]
                if (spec) {
                  form.setFieldsValue({ base_url: spec.default_base_url, capabilities: spec.capabilities })
                }
              }}
              options={providers.map((p) => ({ value: p.key, label: p.label }))}
            />
          </Form.Item>

          <Form.Item name="name" label="备注名称" rules={[{ required: true, message: '请输入备注名称' }]}>
            <Input placeholder="例如：DeepSeek 主账号" maxLength={128} />
          </Form.Item>

          <Form.Item
            name="api_key"
            label="API Key"
            rules={editing ? [] : [{ required: true, message: '请输入 API Key' }]}
            extra={editing ? '留空表示不修改现有 Key' : undefined}
          >
            <Input.Password placeholder="sk-..." autoComplete="off" />
          </Form.Item>

          <Form.Item name="base_url" label="Base URL（高级）">
            <Input placeholder="留空使用厂商默认地址" />
          </Form.Item>

          <Form.Item name="capabilities" label="可用能力">
            <Checkbox.Group
              options={availableCaps.map((cap) => ({ value: cap, label: CAP_LABEL[cap] ?? cap }))}
            />
          </Form.Item>

          <Form.Item name="is_default" valuePropName="checked" style={{ marginBottom: 0 }}>
            <Checkbox>
              <Space size={4}>
                <span>设为该厂商的默认密钥</span>
                <ApiOutlined style={{ color: '#8a93a6' }} />
              </Space>
            </Checkbox>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
