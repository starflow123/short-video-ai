import { useCallback, useEffect, useMemo, useState } from 'react'
import { App, Button, Empty, Input, List, Modal, Select, Space, Tag, Typography } from 'antd'
import { FileTextOutlined, SearchOutlined, ThunderboltOutlined } from '@ant-design/icons'

import { templateApi } from '@/api/endpoints'
import { describeError } from '@/api/client'
import type { PromptTemplate, TemplateCategoryGroup } from '@/api/types'

interface Props {
  /** 填充回调：把渲染后的模板内容写进目标输入框 */
  onInsert: (text: string) => void
  /** 渲染变量（如 { description, visual_style, aspect_ratio }），用于把模板里的占位符替换成当前上下文 */
  context?: Record<string, string>
  /** 按生成类型预筛分类组：image（画面/角色等）/ video */
  scope?: 'image' | 'video' | 'all'
  buttonText?: string
  size?: 'small' | 'middle'
  type?: 'default' | 'link' | 'text'
}

/** 「插入提示词模板」：挑一个模板 → 用当前镜头上下文渲染 → 一键填进提示词输入框。 */
export default function PromptTemplatePicker({
  onInsert,
  context,
  scope = 'all',
  buttonText = '插入模板',
  size = 'small',
  type = 'link',
}: Props) {
  const { message } = App.useApp()
  const [open, setOpen] = useState(false)
  const [groups, setGroups] = useState<TemplateCategoryGroup[]>([])
  const [templates, setTemplates] = useState<PromptTemplate[]>([])
  const [loading, setLoading] = useState(false)
  const [category, setCategory] = useState('')
  const [keyword, setKeyword] = useState('')
  const [busyId, setBusyId] = useState<number | null>(null)

  const loadTemplates = useCallback(async () => {
    setLoading(true)
    try {
      setTemplates(
        await templateApi.list({
          ...(category ? { category } : {}),
          ...(keyword.trim() ? { q: keyword.trim() } : {}),
        }),
      )
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setLoading(false)
    }
  }, [category, keyword, message])

  useEffect(() => {
    if (!open) return
    void (async () => {
      try {
        const list = groups.length ? groups : await templateApi.categories()
        setGroups(list)
        // 按 scope 预选一个合适的分类，减少来回点选
        if (!category) {
          const flat = list.flatMap((group) => group.categories.map((item) => item.key))
          const preferred =
            scope === 'video'
              ? flat.find((key) => key.startsWith('video'))
              : scope === 'image'
                ? flat.find((key) => key.startsWith('frame'))
                : undefined
          setCategory(preferred ?? '')
        }
      } catch (error) {
        message.error(describeError(error))
      }
    })()
  }, [open, scope, groups, category, message])

  useEffect(() => {
    if (open) void loadTemplates()
  }, [open, loadTemplates])

  const categoryOptions = useMemo(() => {
    const flat = groups.flatMap((group) =>
      group.categories.map((item) => ({ value: item.key, label: `${group.label}/${item.label}` })),
    )
    return [{ value: '', label: '全部' }, ...flat].slice(0, scope === 'all' ? 40 : 40)
  }, [groups])

  const use = async (template: PromptTemplate) => {
    setBusyId(template.id)
    try {
      const { content } = await templateApi.render(template.id, context ?? {})
      if (!content.trim()) {
        message.warning('模板渲染结果为空，请检查模板内容与变量')
        return
      }
      onInsert(content.trim())
      setOpen(false)
      message.success(`已套用模板「${template.name}」，可继续手动微调`)
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setBusyId(null)
    }
  }

  return (
    <>
      <Button
        size={size}
        type={type}
        icon={<FileTextOutlined />}
        onClick={() => setOpen(true)}
        style={type === 'link' ? { padding: 0, height: 'auto', fontSize: 12 } : undefined}
      >
        {buttonText}
      </Button>
      <Modal
        title="插入提示词模板"
        open={open}
        onCancel={() => setOpen(false)}
        footer={null}
        width="min(760px, 94vw)"
        destroyOnClose
      >
        <Space.Compact style={{ width: '100%', marginBottom: 12 }}>
          <Select
            size="small"
            showSearch
            optionFilterProp="label"
            value={category}
            onChange={(value) => setCategory(value)}
            options={categoryOptions}
            placeholder="选择模板分类"
            style={{ width: 200 }}
          />
          <Input
            allowClear
            size="small"
            placeholder="搜索模板名称 / 内容"
            prefix={<SearchOutlined />}
            value={keyword}
            onChange={(event) => setKeyword(event.target.value)}
            onPressEnter={() => void loadTemplates()}
            style={{ flex: 1 }}
          />
        </Space.Compact>
        <List
          loading={loading}
          dataSource={templates}
          locale={{ emptyText: <Empty description="没有匹配的模板，可到「提示词模板」页新建" /> }}
          style={{ maxHeight: '58vh', overflowY: 'auto' }}
          renderItem={(item) => (
            <List.Item
              actions={[
                <Button
                  key="use"
                  type="primary"
                  size="small"
                  icon={<ThunderboltOutlined />}
                  loading={busyId === item.id}
                  onClick={() => void use(item)}
                >
                  套用
                </Button>,
              ]}
            >
              <List.Item.Meta
                title={
                  <Space size={6} wrap>
                    <Typography.Text strong style={{ fontSize: 13 }}>
                      {item.name}
                    </Typography.Text>
                    {item.is_system && (
                      <Tag style={{ margin: 0 }} color="blue">
                        内置
                      </Tag>
                    )}
                    {item.category_label && (
                      <Tag style={{ margin: 0 }} bordered={false}>
                        {item.category_label}
                      </Tag>
                    )}
                  </Space>
                }
                description={
                  <Typography.Paragraph
                    type="secondary"
                    ellipsis={{ rows: 2 }}
                    style={{ fontSize: 12, marginBottom: 0 }}
                  >
                    {item.preview || item.content}
                  </Typography.Paragraph>
                }
              />
            </List.Item>
          )}
        />
        <Typography.Text type="secondary" style={{ fontSize: 11.5 }}>
          「套用」会把模板里的变量用当前镜头的画面描述 / 项目风格 / 画幅渲染后填入输入框，覆盖原有内容。
        </Typography.Text>
      </Modal>
    </>
  )
}
