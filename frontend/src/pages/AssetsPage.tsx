import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import {
  App,
  Button,
  Card,
  Col,
  Empty,
  Form,
  Image,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Row,
  Select,
  Space,
  Switch,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import {
  AppstoreOutlined,
  CopyOutlined,
  DeleteOutlined,
  EditOutlined,
  EnvironmentOutlined,
  PictureOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
  SkinOutlined,
  TeamOutlined,
  ThunderboltOutlined,
  ToolOutlined,
} from '@ant-design/icons'
import { useSearchParams } from 'react-router-dom'

import { assetApi, mediaApi, projectApi, taskApi } from '@/api/endpoints'
import CredentialPicker from '@/components/CredentialPicker'
import { useCredentials } from '@/hooks/useCredentials'
import { describeError, mediaUrl } from '@/api/client'
import type { AssetKind, MediaAsset, ProductionAsset, Project } from '@/api/types'

/** 资产类型：与后端 ASSET_KINDS 对齐。角色用来锁「同一个人」，场景 / 道具 / 服装锁环境与物件。 */
const KIND_TABS: { value: string; label: string; icon: ReactNode }[] = [
  { value: 'all', label: '全部资产', icon: <AppstoreOutlined /> },
  { value: 'actor', label: '角色', icon: <TeamOutlined /> },
  { value: 'scene', label: '场景', icon: <EnvironmentOutlined /> },
  { value: 'prop', label: '道具', icon: <ToolOutlined /> },
  { value: 'costume', label: '服装', icon: <SkinOutlined /> },
]

const KIND_LABEL: Record<string, { text: string; color: string }> = {
  actor: { text: '角色', color: 'blue' },
  scene: { text: '场景', color: 'green' },
  prop: { text: '道具', color: 'orange' },
  costume: { text: '服装', color: 'purple' },
}

/** 卡片无参考图时的占位图标：按类型区分，方便一眼分辨角色 / 场景 / 道具 / 服装。 */
const KIND_PLACEHOLDER: Record<string, ReactNode> = {
  actor: <TeamOutlined />,
  scene: <EnvironmentOutlined />,
  prop: <ToolOutlined />,
  costume: <SkinOutlined />,
}

interface AssetForm {
  kind: AssetKind
  name: string
  description?: string
  prompt?: string
  tags?: string
  project_id?: number
  media_id?: number
  is_global?: boolean
}

interface AssetGenForm {
  prompt: string
  use_reference: boolean
  n: number
  aspect_ratio: string
  credential_id?: number
}

/** 出图默认画幅：角色/服装用竖幅，场景用横幅，道具用方图。 */
const GEN_RATIO: Record<string, string> = {
  actor: '3:4',
  costume: '3:4',
  scene: '16:9',
  prop: '1:1',
}

/** 资产卡片：有参考图时用参考图做封面，没有则用类型图标占位。 */
function AssetCard({
  asset,
  onEdit,
  onDelete,
  onCopy,
  onGenerate,
}: {
  asset: ProductionAsset
  onEdit: () => void
  onDelete: () => void
  onCopy: () => void
  onGenerate: () => void
}) {
  const kind = KIND_LABEL[asset.kind] ?? { text: asset.kind, color: 'default' }
  return (
    <Card
      className="hover-card"
      styles={{ body: { padding: 12 } }}
      cover={
        asset.media_url ? (
          <Image
            src={mediaUrl(asset.media_url)}
            alt={asset.name}
            style={{ aspectRatio: '4 / 3', objectFit: 'cover' }}
            wrapperStyle={{ display: 'block' }}
          />
        ) : (
          <div
            style={{
              aspectRatio: '4 / 3',
              display: 'grid',
              placeItems: 'center',
              background: 'linear-gradient(135deg, #eef1f8, #e2e7f3)',
              color: '#8a93a8',
              fontSize: 26,
            }}
          >
            {KIND_PLACEHOLDER[asset.kind] ?? <PictureOutlined />}
          </div>
        )
      }
      actions={[
        <Tooltip key="gen" title={asset.media_url ? '改参考图（保留五官/造型）' : '生成参考图'}>
          <Button type="text" icon={<ThunderboltOutlined />} onClick={onGenerate} />
        </Tooltip>,
        <Tooltip key="copy" title="复制外观描述">
          <Button type="text" icon={<CopyOutlined />} onClick={onCopy} />
        </Tooltip>,
        <Tooltip key="edit" title="编辑">
          <Button type="text" icon={<EditOutlined />} onClick={onEdit} />
        </Tooltip>,
        <Popconfirm key="del" title="删除该资产？" okText="删除" cancelText="取消" onConfirm={onDelete}>
          <Button type="text" danger icon={<DeleteOutlined />} />
        </Popconfirm>,
      ]}
    >
      <Space size={6} style={{ marginBottom: 6 }} wrap>
        <Tag color={kind.color} style={{ margin: 0 }}>
          {kind.text}
        </Tag>
        {asset.is_global && (
          <Tag color="gold" style={{ margin: 0 }}>
            跨项目
          </Tag>
        )}
        {asset.source === 'generated' && (
          <Tag style={{ margin: 0 }} color="cyan">
            自动提取
          </Tag>
        )}
      </Space>
      <Typography.Text strong ellipsis style={{ display: 'block' }}>
        {asset.name}
      </Typography.Text>
      <Typography.Text type="secondary" ellipsis style={{ display: 'block', fontSize: 12.5, marginTop: 4 }}>
        {asset.prompt || asset.description || '暂无外观描述，建议补上以便跨镜头复用'}
      </Typography.Text>
      <Typography.Text type="secondary" style={{ display: 'block', fontSize: 11.5, marginTop: 6 }}>
        {asset.project_title || '未关联项目'}
      </Typography.Text>
    </Card>
  )
}

export default function AssetsPage() {
  const { message } = App.useApp()
  const [searchParams] = useSearchParams()
  const [projects, setProjects] = useState<Project[]>([])
  // 全量拉取（只按项目 / 关键词过滤），类型分类在前端本地切分：
  // 这样顶部分类栏能直接显示每一类的数量，不必为每个分类各发一次请求。
  const [allAssets, setAllAssets] = useState<ProductionAsset[]>([])
  const [mediaLibrary, setMediaLibrary] = useState<MediaAsset[]>([])
  const [loading, setLoading] = useState(true)
  const [kind, setKind] = useState('all')
  // 支持从项目详情页「管理资产」带着 ?project=<id> 进来，直接落到该项目的资产
  const [projectId, setProjectId] = useState<number | 'all'>(() => {
    const raw = Number(searchParams.get('project'))
    return Number.isFinite(raw) && raw > 0 ? raw : 'all'
  })
  const [keyword, setKeyword] = useState('')

  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<ProductionAsset | null>(null)
  const [saving, setSaving] = useState(false)
  const [form] = Form.useForm<AssetForm>()
  const [pickerOpen, setPickerOpen] = useState(false)
  const [pickerKeyword, setPickerKeyword] = useState('')

  // 参考图生成：弹窗里改描述 / 选择是否在已有参考图上改，提交后轮询任务并刷新封面
  const { byCapability } = useCredentials()
  const imageCreds = byCapability('image')
  const [genAsset, setGenAsset] = useState<ProductionAsset | null>(null)
  const [genOpen, setGenOpen] = useState(false)
  const [genSaving, setGenSaving] = useState(false)
  const [genForm] = Form.useForm<AssetGenForm>()
  const [watchTaskId, setWatchTaskId] = useState<number | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [projectList, assetList] = await Promise.all([
        projectApi.list(),
        assetApi.list({
          ...(projectId === 'all' ? {} : { project_id: projectId }),
          ...(keyword.trim() ? { keyword: keyword.trim() } : {}),
        }),
      ])
      setProjects(projectList)
      setAllAssets(assetList)
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setLoading(false)
    }
  }, [projectId, keyword, message])

  useEffect(() => {
    void load()
  }, [load])

  const assets = useMemo(
    () => (kind === 'all' ? allAssets : allAssets.filter((item) => item.kind === kind)),
    [allAssets, kind],
  )

  /** 分类栏上的数量：按当前「项目 / 关键词」范围统计，切换分类时数字不变，方便判断哪类还没建。 */
  const counts = useMemo(() => {
    const map: Record<string, number> = { all: allAssets.length }
    for (const item of allAssets) map[item.kind] = (map[item.kind] ?? 0) + 1
    return map
  }, [allAssets])

  // 素材库里的图片用于「选参考图」；只在需要时拉一次
  const loadMedia = useCallback(async () => {
    try {
      const list = await mediaApi.list({ kind: 'image' })
      setMediaLibrary(list)
    } catch (error) {
      message.error(describeError(error))
    }
  }, [message])

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue({ kind: 'actor', is_global: false, project_id: projectId === 'all' ? undefined : projectId })
    setFormOpen(true)
    void loadMedia()
  }

  /** 打开参考图生成弹窗：默认带入资产描述；已有参考图时默认走「图上改」（保脸换装）。 */
  const openGenerate = (asset: ProductionAsset) => {
    setGenAsset(asset)
    genForm.setFieldsValue({
      prompt: asset.prompt || asset.description || '',
      use_reference: !!asset.media_id,
      n: 1,
      aspect_ratio: GEN_RATIO[asset.kind] ?? '3:4',
      credential_id: undefined,
    })
    setGenOpen(true)
    if (!mediaLibrary.length) void loadMedia()
  }

  const submitGenerate = async () => {
    if (!genAsset) return
    try {
      const values = await genForm.validateFields()
      setGenSaving(true)
      const task = await assetApi.generate(genAsset.id, {
        prompt: values.prompt.trim(),
        credential_id: values.credential_id,
        use_reference: values.use_reference,
        n: values.n,
        aspect_ratio: values.aspect_ratio,
      })
      setGenOpen(false)
      message.success('已提交出图，进度见右下角；完成后封面会自动刷新')
      setWatchTaskId(task.id)
    } catch (error) {
      if ((error as any)?.errorFields) return
      message.error(describeError(error))
    } finally {
      setGenSaving(false)
    }
  }

  // 轮询本次出图任务：结束（完成 / 失败 / 取消）后刷新列表，让新参考图直接成为封面
  useEffect(() => {
    if (!watchTaskId) return
    let stopped = false
    let timer: number | undefined

    const tick = async () => {
      try {
        const task = await taskApi.get(watchTaskId)
        if (stopped) return
        if (task.status === 'completed') {
          setWatchTaskId(null)
          message.success('参考图已生成并挂到资产上')
          await load()
          return
        }
        if (task.status === 'failed' || task.status === 'canceled') {
          setWatchTaskId(null)
          message.error(task.error || (task.status === 'canceled' ? '出图已取消' : '出图失败'))
          await load()
          return
        }
      } catch {
        // 单次轮询失败不终止，等下一个周期
      }
      if (!stopped) timer = window.setTimeout(tick, 3000)
    }

    void tick()
    return () => {
      stopped = true
      if (timer) window.clearTimeout(timer)
    }
  }, [watchTaskId, load, message])

  const openEdit = (asset: ProductionAsset) => {
    setEditing(asset)
    form.setFieldsValue({
      kind: asset.kind as AssetKind,
      name: asset.name,
      description: asset.description ?? undefined,
      prompt: asset.prompt ?? undefined,
      tags: (asset.tags ?? []).join('、'),
      project_id: asset.project_id ?? undefined,
      media_id: asset.media_id ?? undefined,
      is_global: asset.is_global,
    })
    setFormOpen(true)
    void loadMedia()
  }

  const submit = async () => {
    try {
      const values = await form.validateFields()
      setSaving(true)
      const payload = {
        kind: values.kind,
        name: values.name.trim(),
        description: values.description ?? null,
        prompt: values.prompt ?? null,
        tags: (values.tags || '')
          .split(/[、,，\s]+/)
          .map((item) => item.trim())
          .filter(Boolean),
        project_id: values.project_id ?? null,
        media_id: values.media_id ?? null,
        is_global: !!values.is_global,
      }
      if (editing) {
        await assetApi.update(editing.id, payload)
        message.success('资产已更新')
      } else {
        await assetApi.create(payload)
        message.success('资产已创建，可在生成关键帧 / 视频时关联复用')
      }
      setFormOpen(false)
      await load()
    } catch (error) {
      if ((error as any)?.errorFields) return
      message.error(describeError(error))
    } finally {
      setSaving(false)
    }
  }

  const remove = async (id: number) => {
    try {
      await assetApi.remove(id)
      message.success('资产已删除（参考图仍保留在素材库）')
      setAllAssets((prev) => prev.filter((item) => item.id !== id))
    } catch (error) {
      message.error(describeError(error))
    }
  }

  const copyPrompt = async (asset: ProductionAsset) => {
    const text = asset.prompt || asset.description || ''
    if (!text) {
      message.warning('该资产还没有外观描述')
      return
    }
    try {
      await navigator.clipboard.writeText(text)
      message.success('外观描述已复制，可直接粘贴到提示词里')
    } catch {
      message.warning('浏览器不允许自动复制，请手动选中复制')
    }
  }

  const filteredMedia = useMemo(() => {
    const text = pickerKeyword.trim().toLowerCase()
    if (!text) return mediaLibrary.slice(0, 60)
    return mediaLibrary
      .filter((item) => (item.filename || '').toLowerCase().includes(text))
      .slice(0, 60)
  }, [mediaLibrary, pickerKeyword])

  const currentMediaId = Form.useWatch('media_id', form)

  return (
    <div>
      <div className="page-head">
        <div>
          <h2 className="page-title">资产管理</h2>
          <p className="page-sub">
            沉淀固定角色 / 场景 / 道具 / 服装的外观描述与参考图。生成关键帧或视频时关联资产，
            即可让不同镜头里都是同一个人、同一个场景，画风与细节也保持一致。
          </p>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
          新增资产
        </Button>
      </div>

      {/* 分类栏放在页面顶部最显眼处，并带数量，方便一眼看清每类各有多少 */}
      <div className="kind-bar" style={{ marginBottom: 16 }}>
        {KIND_TABS.map((tab) => (
          <button
            key={tab.value}
            type="button"
            className={`kind-pill${kind === tab.value ? ' active' : ''}`}
            onClick={() => setKind(tab.value)}
          >
            <span className="kind-icon">{tab.icon}</span>
            {tab.label}
            <span className="kind-count">{counts[tab.value] ?? 0}</span>
          </button>
        ))}
      </div>

      <Card style={{ marginBottom: 16 }} styles={{ body: { padding: 12 } }}>
        <Space wrap size={12}>
          <Select
            value={projectId}
            style={{ width: 200 }}
            onChange={(value) => setProjectId(value)}
            options={[
              { value: 'all', label: '全部项目' },
              ...projects.map((project) => ({ value: project.id, label: project.title })),
            ]}
          />
          <Input
            allowClear
            style={{ width: 240 }}
            placeholder="按名称 / 描述搜索"
            prefix={<SearchOutlined />}
            value={keyword}
            onChange={(event) => setKeyword(event.target.value)}
            onPressEnter={() => void load()}
          />
          <Button icon={<ReloadOutlined />} onClick={() => void load()}>
            刷新
          </Button>
          <Typography.Text type="secondary" style={{ fontSize: 12.5 }}>
            共 {assets.length} 个资产
          </Typography.Text>
        </Space>
      </Card>

      {loading ? null : assets.length === 0 ? (
        <Card>
          <Empty description="还没有资产，点右上角「新增资产」建立角色档案，或从素材库把满意的图存为资产" />
        </Card>
      ) : (
        <Row gutter={[16, 16]}>
          {assets.map((asset) => (
            <Col key={asset.id} xs={24} sm={12} md={8} lg={6} xxl={4}>
              <AssetCard
                asset={asset}
                onEdit={() => openEdit(asset)}
                onDelete={() => void remove(asset.id)}
                onCopy={() => void copyPrompt(asset)}
                onGenerate={() => openGenerate(asset)}
              />
            </Col>
          ))}
        </Row>
      )}

      <Modal
        title={editing ? `编辑资产 · ${editing.name}` : '新增资产'}
        open={formOpen}
        onCancel={() => setFormOpen(false)}
        onOk={() => void submit()}
        confirmLoading={saving}
        okText="保存"
        cancelText="取消"
        width="min(720px, 92vw)"
        destroyOnClose
      >
        <Form form={form} layout="vertical" requiredMark={false}>
          <Row gutter={16}>
            <Col xs={24} md={12}>
              <Form.Item name="kind" label="资产类型" rules={[{ required: true }]}>
                <Select
                  options={[
                    { value: 'actor', label: '角色（锁人物长相与服装）' },
                    { value: 'scene', label: '场景（锁环境与光线）' },
                    { value: 'prop', label: '道具（锁关键物件）' },
                    { value: 'costume', label: '服装（锁一套造型）' },
                  ]}
                />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item name="name" label="名称" rules={[{ required: true, message: '请填写名称' }]}>
                <Input placeholder="如：林晚 / 老宅客厅 / 怀表" maxLength={200} />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item
            name="prompt"
            label="外观描述（会原样拼进生成提示词，越具体越稳定）"
            tooltip="写清性别、年龄、脸型、发型、体型、肤色、辨识特征，以及服装颜色 / 款式 / 材质"
          >
            <Input.TextArea
              rows={4}
              placeholder="如：25 岁女性，鹅蛋脸，黑色长直发及腰，眉形细长，身高 165cm 偏瘦，肤色白皙；穿藏青色丝质衬衫与米白色阔腿长裤"
            />
          </Form.Item>

          <Row gutter={16}>
            <Col xs={24} md={12}>
              <Form.Item name="description" label="备注（给自己看的简介）">
                <Input placeholder="如：女主，第 1 集出场" />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item name="tags" label="标签（、或空格分隔）">
                <Input placeholder="女主 都市 冷静" />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col xs={24} md={12}>
              <Form.Item name="project_id" label="所属项目">
                <Select
                  allowClear
                  placeholder="不选则不归属任何项目"
                  options={projects.map((project) => ({ value: project.id, label: project.title }))}
                />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item name="is_global" label="跨项目复用" valuePropName="checked">
                <Switch />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item label="参考图（可选）">
            <Space>
              <Button icon={<PictureOutlined />} onClick={() => setPickerOpen(true)}>
                从素材库选择
              </Button>
              {currentMediaId ? (
                <Space size={8}>
                  <Image
                    src={mediaUrl(mediaLibrary.find((item) => item.id === currentMediaId)?.url || '')}
                    width={54}
                    height={54}
                    style={{ objectFit: 'cover', borderRadius: 6 }}
                  />
                  <Button size="small" type="link" onClick={() => form.setFieldsValue({ media_id: undefined })}>
                    移除
                  </Button>
                </Space>
              ) : (
                <Typography.Text type="secondary" style={{ fontSize: 12.5 }}>
                  未选择
                </Typography.Text>
              )}
            </Space>
            <Form.Item name="media_id" noStyle hidden>
              <Input />
            </Form.Item>
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={genAsset ? `${genAsset.media_url ? '修改' : '生成'}参考图 · ${genAsset.name}` : '生成参考图'}
        open={genOpen}
        onCancel={() => setGenOpen(false)}
        onOk={() => void submitGenerate()}
        okText="开始生成"
        cancelText="取消"
        confirmLoading={genSaving}
        width="min(560px, 94vw)"
        destroyOnClose
      >
        <Form form={genForm} layout="vertical" requiredMark={false}>
          <Form.Item
            name="prompt"
            label="外观描述（会作为出图提示词）"
            rules={[{ required: true, message: '请填写要生成的外观描述' }]}
          >
            <Input.TextArea
              rows={5}
              placeholder="例如：18 岁男高中生，短发，白色衬衫配深色外套，清瘦，眼神警觉，正面半身，写实电影感"
            />
          </Form.Item>

          {genAsset?.media_url && (
            <Form.Item
              name="use_reference"
              label="基于当前参考图修改"
              valuePropName="checked"
              extra="开启后只改服装 / 造型 / 角度，保留五官、脸型与发型，适合换装不换人。"
            >
              <Switch />
            </Form.Item>
          )}

          <Row gutter={12}>
            <Col span={8}>
              <Form.Item name="aspect_ratio" label="画幅">
                <Select
                  options={[
                    { value: '3:4', label: '3:4 竖幅' },
                    { value: '9:16', label: '9:16 竖屏' },
                    { value: '16:9', label: '16:9 横幅' },
                    { value: '1:1', label: '1:1 方图' },
                    { value: '4:3', label: '4:3' },
                  ]}
                />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="n" label="生成张数">
                <InputNumber min={1} max={4} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item
                name="credential_id"
                label="密钥"
                extra={imageCreds.length === 0 ? '未配置图片密钥，将使用默认服务商' : undefined}
              >
                <CredentialPicker credentials={imageCreds} preferProvider="agnes" />
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Modal>

      <Modal
        title="从素材库选择参考图"
        open={pickerOpen}
        onCancel={() => setPickerOpen(false)}
        footer={null}
        width="min(880px, 94vw)"
        destroyOnClose
      >
        <Input
          allowClear
          placeholder="按文件名搜索"
          prefix={<SearchOutlined />}
          value={pickerKeyword}
          onChange={(event) => setPickerKeyword(event.target.value)}
          style={{ marginBottom: 12 }}
        />
        {filteredMedia.length === 0 ? (
          <Empty description="素材库暂无图片，可先在项目里生成关键帧" />
        ) : (
          <Row gutter={[12, 12]} style={{ maxHeight: '56vh', overflowY: 'auto' }}>
            {filteredMedia.map((item) => (
              <Col key={item.id} xs={12} sm={8} md={6}>
                <div
                  className="hover-card"
                  style={{ cursor: 'pointer', border: '1px solid #eef0f5', borderRadius: 8, overflow: 'hidden' }}
                  onClick={() => {
                    form.setFieldsValue({ media_id: item.id })
                    setPickerOpen(false)
                  }}
                >
                  <Image src={mediaUrl(item.url)} style={{ aspectRatio: '1 / 1', objectFit: 'cover' }} preview={false} />
                  <Typography.Text
                    type="secondary"
                    ellipsis
                    style={{ display: 'block', fontSize: 11.5, padding: '4px 6px' }}
                  >
                    {item.filename}
                  </Typography.Text>
                </div>
              </Col>
            ))}
          </Row>
        )}
      </Modal>
    </div>
  )
}
