import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  App,
  Button,
  Card,
  Col,
  Divider,
  Empty,
  Form,
  Input,
  List,
  Menu,
  Modal,
  Popconfirm,
  Row,
  Select,
  Space,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import {
  CopyOutlined,
  DeleteOutlined,
  FileTextOutlined,
  PlusOutlined,
  ReloadOutlined,
  SaveOutlined,
  SearchOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'

import { templateApi } from '@/api/endpoints'
import { describeError } from '@/api/client'
import type { PromptTemplate, TemplateCategoryGroup } from '@/api/types'

interface Draft {
  name: string
  category: string
  preview?: string
  content: string
}

const EMPTY_DRAFT: Draft = { name: '', category: '', preview: '', content: '' }

/** 分类树 + 搜索 + 模板编辑/预览。内置模板只读，可「另存为我的模板」后再改。 */
export default function PromptsPage() {
  const { message } = App.useApp()
  const [groups, setGroups] = useState<TemplateCategoryGroup[]>([])
  const [templates, setTemplates] = useState<PromptTemplate[]>([])
  const [loading, setLoading] = useState(true)
  const [category, setCategory] = useState<string>('')
  const [keyword, setKeyword] = useState('')

  const [selected, setSelected] = useState<PromptTemplate | null>(null)
  const [isNew, setIsNew] = useState(false)
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT)
  const [saving, setSaving] = useState(false)

  const [varValues, setVarValues] = useState<Record<string, string>>({})
  const [rendered, setRendered] = useState('')
  const [renderOpen, setRenderOpen] = useState(false)
  const [rendering, setRendering] = useState(false)

  const loadCategories = useCallback(async () => {
    try {
      setGroups(await templateApi.categories())
    } catch (error) {
      message.error(describeError(error))
    }
  }, [message])

  const loadTemplates = useCallback(async () => {
    setLoading(true)
    try {
      const list = await templateApi.list({
        ...(category ? { category } : {}),
        ...(keyword.trim() ? { q: keyword.trim() } : {}),
      })
      setTemplates(list)
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setLoading(false)
    }
  }, [category, keyword, message])

  useEffect(() => {
    void loadCategories()
  }, [loadCategories])

  useEffect(() => {
    void loadTemplates()
  }, [loadTemplates])

  // 分类树：一级分组（画面/视频/角色…）+ 二级分类
  const menuItems = useMemo(() => {
    const items: any[] = [{ key: '', label: '全部模板', icon: <FileTextOutlined /> }]
    for (const group of groups) {
      items.push({
        key: `group-${group.key}`,
        type: 'group',
        label: group.label,
        children: group.categories.map((item) => ({
          key: item.key,
          label: item.label,
        })),
      })
    }
    return items
  }, [groups])

  const categoryLabel = useMemo(() => {
    for (const group of groups) {
      const hit = group.categories.find((item) => item.key === category)
      if (hit) return hit.label
    }
    return '全部模板'
  }, [groups, category])

  const openTemplate = (template: PromptTemplate) => {
    setSelected(template)
    setIsNew(false)
    setDraft({
      name: template.name,
      category: template.category,
      preview: template.preview ?? '',
      content: template.content,
    })
    setVarValues({})
    setRendered('')
  }

  const openNew = () => {
    const firstCategory = groups[0]?.categories[0]?.key ?? 'frame_key_image'
    setSelected(null)
    setIsNew(true)
    setDraft({ ...EMPTY_DRAFT, category })
    if (!category) setDraft({ ...EMPTY_DRAFT, category: firstCategory })
  }

  const save = async () => {
    if (!draft.name.trim() || !draft.content.trim()) {
      message.warning('请填写模板名称与模板内容')
      return
    }
    setSaving(true)
    try {
      if (isNew) {
        const created = await templateApi.create({
          category: draft.category,
          name: draft.name.trim(),
          preview: draft.preview ?? null,
          content: draft.content,
        })
        message.success('模板已创建')
        setSelected(created)
        setIsNew(false)
      } else if (selected) {
        const updated = await templateApi.update(selected.id, {
          category: draft.category,
          name: draft.name.trim(),
          preview: draft.preview ?? null,
          content: draft.content,
        })
        message.success('模板已保存')
        setSelected(updated)
      }
      await loadTemplates()
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setSaving(false)
    }
  }

  const saveAsMine = async () => {
    if (!selected) return
    try {
      const created = await templateApi.create({
        category: selected.category,
        name: `${selected.name}（副本）`,
        preview: selected.preview ?? null,
        content: selected.content,
      })
      message.success('已另存为我的模板')
      setSelected(created)
      setIsNew(false)
      setDraft({
        name: created.name,
        category: created.category,
        preview: created.preview ?? '',
        content: created.content,
      })
      await loadTemplates()
    } catch (error) {
      message.error(describeError(error))
    }
  }

  const remove = async () => {
    if (!selected) return
    try {
      await templateApi.remove(selected.id)
      message.success('模板已删除')
      setSelected(null)
      setIsNew(false)
      setDraft(EMPTY_DRAFT)
      await loadTemplates()
    } catch (error) {
      message.error(describeError(error))
    }
  }

  const doRender = async () => {
    if (!selected) {
      message.info('先保存模板后才能渲染预览')
      return
    }
    setRendering(true)
    try {
      const result = await templateApi.render(selected.id, varValues)
      setRendered(result.content)
      setRenderOpen(true)
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setRendering(false)
    }
  }

  const variables = selected?.variables ?? []
  const readOnly = !!selected?.is_system

  return (
    <div>
      <div className="page-head">
        <div>
          <h2 className="page-title">提示词模板</h2>
          <p className="page-sub">
            沉淀经过验证的分镜 / 视频 / 角色提示词写法。内置模板覆盖画面、镜头运动、角色建档等常见场景，
            可直接「另存为我的模板」再按自己的风格调整；生成时一键插入到任意提示词字段。
          </p>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => void loadTemplates()}>
            刷新
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={openNew}>
            新建模板
          </Button>
        </Space>
      </div>

      <Row gutter={16}>
        <Col xs={24} lg={7} xl={6}>
          <Card styles={{ body: { padding: 12 } }}>
            <Input
              allowClear
              placeholder="搜索模板名称 / 内容"
              prefix={<SearchOutlined />}
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
              onPressEnter={() => void loadTemplates()}
            />
            <Divider style={{ margin: '10px 0' }} />
            <Menu
              mode="inline"
              selectedKeys={[category]}
              items={menuItems}
              onClick={({ key }) => setCategory(String(key))}
              style={{ borderInlineEnd: 'none', maxHeight: 260, overflowY: 'auto' }}
            />
            <Divider style={{ margin: '10px 0' }} />
            <List
              size="small"
              loading={loading}
              dataSource={templates}
              locale={{ emptyText: <Empty description="该分类下暂无模板" /> }}
              renderItem={(item) => (
                <List.Item
                  className="hover-card"
                  style={{
                    cursor: 'pointer',
                    padding: '8px 10px',
                    borderRadius: 8,
                    background: selected?.id === item.id ? '#f0f5ff' : undefined,
                  }}
                  onClick={() => openTemplate(item)}
                >
                  <Space direction="vertical" size={2} style={{ width: '100%' }}>
                    <Space size={6} wrap>
                      <Typography.Text strong ellipsis style={{ fontSize: 13 }}>
                        {item.name}
                      </Typography.Text>
                      {item.is_system && (
                        <Tag style={{ margin: 0 }} color="blue">
                          内置
                        </Tag>
                      )}
                    </Space>
                    <Typography.Text type="secondary" ellipsis style={{ fontSize: 11.5 }}>
                      {item.preview || item.content.slice(0, 40)}
                    </Typography.Text>
                  </Space>
                </List.Item>
              )}
            />
          </Card>
        </Col>

        <Col xs={24} lg={17} xl={18}>
          <Card
            title={
              isNew ? '新建模板' : selected ? selected.name : '模板编辑'
            }
            extra={
              selected && (
                <Space>
                  {selected.is_system && (
                    <Tooltip title="基于内置模板创建一份可编辑的副本">
                      <Button icon={<CopyOutlined />} onClick={() => void saveAsMine()}>
                        另存为我的模板
                      </Button>
                    </Tooltip>
                  )}
                  <Button
                    icon={<ThunderboltOutlined />}
                    disabled={isNew || !selected}
                    loading={rendering}
                    onClick={() => void doRender()}
                  >
                    渲染预览
                  </Button>
                  <Popconfirm
                    title="删除该模板？"
                    okText="删除"
                    cancelText="取消"
                    disabled={readOnly || isNew}
                    onConfirm={() => void remove()}
                  >
                    <Button danger icon={<DeleteOutlined />} disabled={readOnly || isNew} />
                  </Popconfirm>
                  <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={() => void save()}>
                    保存
                  </Button>
                </Space>
              )
            }
          >
            {!selected && !isNew ? (
              <Empty description="从左侧选择一个模板开始编辑，或点右上角「新建模板」" />
            ) : (
              <Form layout="vertical" requiredMark={false}>
                {readOnly && (
                  <Typography.Text type="warning" style={{ display: 'block', marginBottom: 12, fontSize: 12.5 }}>
                    这是系统内置模板，内容不可直接修改。点「另存为我的模板」即可基于它创建自己的版本。
                  </Typography.Text>
                )}
                <Row gutter={16}>
                  <Col xs={24} md={14}>
                    <Form.Item label="模板名称">
                      <Input
                        value={draft.name}
                        maxLength={200}
                        disabled={readOnly}
                        onChange={(event) => setDraft({ ...draft, name: event.target.value })}
                        placeholder={`如：${categoryLabel}·电影感光影`}
                      />
                    </Form.Item>
                  </Col>
                  <Col xs={24} md={10}>
                    <Form.Item label="所属分类">
                      <Select
                        value={draft.category}
                        disabled={readOnly}
                        onChange={(value) => setDraft({ ...draft, category: value })}
                        options={groups.flatMap((group) =>
                          group.categories.map((item) => ({
                            value: item.key,
                            label: `${group.label} / ${item.label}`,
                          })),
                        )}
                      />
                    </Form.Item>
                  </Col>
                </Row>
                <Form.Item label="一句话简介（列表里展示）">
                  <Input
                    value={draft.preview}
                    disabled={readOnly}
                    maxLength={255}
                    onChange={(event) => setDraft({ ...draft, preview: event.target.value })}
                    placeholder="如：强反差侧逆光，人物轮廓清晰、背景压暗"
                  />
                </Form.Item>
                <Form.Item
                  label="模板内容"
                  tooltip={'支持 {{变量名}} 占位与 {% if "关键词" in 变量名.lower() %}...{% else %}...{% endif %} 条件片段'}
                >
                  <Input.TextArea
                    rows={14}
                    value={draft.content}
                    disabled={readOnly}
                    onChange={(event) => setDraft({ ...draft, content: event.target.value })}
                    placeholder="示例：{{description}}。{{visual_style}}；电影级光影，主光来自左上方..."
                    style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: 12.5 }}
                  />
                </Form.Item>
                <Space size={8} wrap>
                  <Typography.Text type="secondary" style={{ fontSize: 12.5 }}>
                    可用变量：
                  </Typography.Text>
                  {(selected?.variables ?? []).length === 0 ? (
                    <Typography.Text type="secondary" style={{ fontSize: 12.5 }}>
                      保存后自动解析（在内容里写 {'{{变量名}}'}）
                    </Typography.Text>
                  ) : (
                    (selected?.variables ?? []).map((name) => (
                      <Tag key={name} color="geekblue" style={{ margin: 0 }}>
                        {`{{${name}}}`}
                      </Tag>
                    ))
                  )}
                </Space>
              </Form>
            )}
          </Card>
        </Col>
      </Row>

      <Modal
        title="渲染预览"
        open={renderOpen}
        onCancel={() => setRenderOpen(false)}
        footer={
          <Button
            type="primary"
            onClick={() => {
              void navigator.clipboard
                .writeText(rendered)
                .then(() => message.success('已复制渲染结果'))
                .catch(() => message.warning('浏览器不允许自动复制'))
            }}
          >
            复制结果
          </Button>
        }
        width="min(760px, 92vw)"
        destroyOnClose
      >
        {variables.length > 0 && (
          <>
            <Typography.Text type="secondary" style={{ fontSize: 12.5 }}>
              试填变量（不填则渲染为空串）：
            </Typography.Text>
            <Row gutter={[12, 8]} style={{ marginTop: 8 }}>
              {variables.map((name) => (
                <Col xs={24} md={12} key={name}>
                  <Input
                    size="small"
                    addonBefore={name}
                    value={varValues[name] ?? ''}
                    onChange={(event) => setVarValues({ ...varValues, [name]: event.target.value })}
                    onBlur={() => void doRender()}
                  />
                </Col>
              ))}
            </Row>
            <Divider style={{ margin: '12px 0' }} />
          </>
        )}
        <Input.TextArea
          rows={12}
          value={rendered}
          readOnly
          style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: 12.5 }}
        />
      </Modal>
    </div>
  )
}
