import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  App,
  Button,
  Card,
  Col,
  Empty,
  Form,
  Image,
  Input,
  Modal,
  Popconfirm,
  Row,
  Segmented,
  Select,
  Space,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import {
  ArrowLeftOutlined,
  DeleteOutlined,
  DownloadOutlined,
  FullscreenOutlined,
  ReloadOutlined,
  TeamOutlined,
} from '@ant-design/icons'
import { useNavigate, useParams } from 'react-router-dom'

import ProjectCards from '@/components/ProjectCards'
import { assetApi, episodeApi, mediaApi, projectApi } from '@/api/endpoints'
import { describeError, mediaUrl } from '@/api/client'
import type { MediaAsset, Project } from '@/api/types'

/** 分镜信息（用于把素材按分镜分组） */
interface ShotInfo {
  episode: string
  order: number
  title?: string | null
  sort: string
}

function formatSize(bytes?: number | null) {
  if (!bytes) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

/** 单个素材卡片（图片 / 视频）。 */
function MediaCard({
  asset,
  onExpand,
  onDelete,
  onSaveAsset,
}: {
  asset: MediaAsset
  onExpand: () => void
  onDelete: () => void
  onSaveAsset?: () => void
}) {
  return (
    <Card
      className="hover-card"
      styles={{ body: { padding: 12 } }}
      cover={
        asset.kind === 'video' ? (
          <video
            src={mediaUrl(asset.url)}
            controls
            preload="metadata"
            style={{ width: '100%', aspectRatio: '1 / 1', objectFit: 'cover', background: '#f1f2f7' }}
          />
        ) : (
          <Image
            src={mediaUrl(asset.url)}
            alt={asset.filename}
            style={{ aspectRatio: '1 / 1', objectFit: 'cover' }}
            wrapperStyle={{ display: 'block' }}
          />
        )
      }
      actions={[
        ...(asset.kind === 'video'
          ? [
              <Tooltip key="expand" title="放大播放">
                <Button type="text" icon={<FullscreenOutlined />} onClick={onExpand} />
              </Tooltip>,
            ]
          : [
              ...(onSaveAsset
                ? [
                    <Tooltip key="asset" title="存为资产（角色 / 场景 / 道具 / 服装），生成时可直接复用">
                      <Button type="text" icon={<TeamOutlined />} onClick={onSaveAsset} />
                    </Tooltip>,
                  ]
                : []),
            ]),
        <Button
          key="download"
          type="text"
          icon={<DownloadOutlined />}
          href={mediaUrl(asset.url)}
          target="_blank"
        />,
        <Popconfirm key="delete" title="删除该素材？" okText="删除" cancelText="取消" onConfirm={onDelete}>
          <Button type="text" danger icon={<DeleteOutlined />} />
        </Popconfirm>,
      ]}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
        <Tag style={{ margin: 0 }}>{asset.kind === 'video' ? '视频' : '图片'}</Tag>
        <Typography.Text type="secondary" style={{ fontSize: 11.5, flexShrink: 0 }}>
          {formatSize(asset.size_bytes)}
        </Typography.Text>
      </div>
      <Typography.Text type="secondary" ellipsis style={{ fontSize: 12, display: 'block', marginTop: 6 }}>
        {asset.provider ? `${asset.provider} · ` : ''}
        {asset.model || asset.filename}
      </Typography.Text>
    </Card>
  )
}

/** 一级页：项目卡片列表。 */
function MediaProjects() {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const [projects, setProjects] = useState<Project[]>([])
  const [assets, setAssets] = useState<MediaAsset[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    void (async () => {
      try {
        const [projectList, assetList] = await Promise.all([projectApi.list(), mediaApi.list()])
        setProjects(projectList)
        setAssets(assetList)
      } catch (error) {
        message.error(describeError(error))
      } finally {
        setLoading(false)
      }
    })()
  }, [message])

  const counts = useMemo(() => {
    const map = new Map<number, { image: number; video: number }>()
    for (const asset of assets) {
      if (asset.project_id == null) continue
      const item = map.get(asset.project_id) ?? { image: 0, video: 0 }
      if (asset.kind === 'video') item.video += 1
      else item.image += 1
      map.set(asset.project_id, item)
    }
    return map
  }, [assets])

  const unlinkedCount = useMemo(
    () => assets.filter((asset) => asset.project_id == null).length,
    [assets],
  )

  return (
    <div>
      <div className="page-head">
        <div>
          <h2 className="page-title">素材库</h2>
          <p className="page-sub">按项目查看生成的关键帧、视频与成片；点开项目看每个分镜的素材。</p>
        </div>
      </div>
      <ProjectCards
        projects={projects}
        loading={loading}
        onOpen={(id) => navigate(`/media/${id}`)}
        emptyText="还没有项目，先去「我的项目」创建一部短剧"
        stats={(project) => {
          const item = counts.get(project.id)
          return [
            { label: '图片', value: item?.image ?? 0 },
            { label: '视频', value: item?.video ?? 0 },
          ]
        }}
      />
      {unlinkedCount > 0 && (
        <Card
          className="hover-card"
          hoverable
          style={{ marginTop: 16 }}
          onClick={() => navigate('/media/unlinked')}
        >
          <Space size={10} wrap>
            <Typography.Text strong>未归属项目的素材</Typography.Text>
            <Tag>{unlinkedCount} 个</Tag>
          </Space>
          <Typography.Paragraph type="secondary" style={{ margin: '6px 0 0', fontSize: 12.5 }}>
            智能剪辑成片、单独上传的素材等不属于任何项目的文件都在这里，点击查看。
          </Typography.Paragraph>
        </Card>
      )}
    </div>
  )
}

/** 二级页：某个项目的素材，按分镜分组。 */
function ProjectMedia({ projectId }: { projectId: number }) {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const [project, setProject] = useState<Project | null>(null)
  const [shots, setShots] = useState<Record<number, ShotInfo>>({})
  const [assets, setAssets] = useState<MediaAsset[]>([])
  const [loading, setLoading] = useState(true)
  const [kind, setKind] = useState('all')
  const [preview, setPreview] = useState<MediaAsset | null>(null)

  // 存为资产：把满意的图沉淀为角色 / 场景 / 道具 / 服装，生成时关联复用
  const [assetTarget, setAssetTarget] = useState<MediaAsset | null>(null)
  const [assetForm] = Form.useForm<{ kind: string; name: string; description?: string }>()
  const [assetSaving, setAssetSaving] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [projectInfo, episodes] = await Promise.all([
        projectApi.get(projectId),
        projectApi.listEpisodes(projectId),
      ])
      const ordered = [...episodes].sort((a, b) => a.order_index - b.order_index)
      const shotLists = await Promise.all(ordered.map((episode) => episodeApi.listShots(episode.id)))
      const map: Record<number, ShotInfo> = {}
      ordered.forEach((episode, index) => {
        shotLists[index].forEach((shot) => {
          map[shot.id] = {
            episode: episode.title,
            order: shot.order_index,
            title: shot.title,
            sort: `${String(episode.order_index).padStart(3, '0')}-${String(shot.order_index).padStart(3, '0')}`,
          }
        })
      })
      setProject(projectInfo)
      setShots(map)
      setAssets(await mediaApi.list({ project_id: projectId, ...(kind === 'all' ? {} : { kind }) }))
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setLoading(false)
    }
  }, [projectId, kind, message])

  useEffect(() => {
    void load()
  }, [load])

  // 按分镜分组：同一分镜的关键帧 / 视频归在一起，未关联分镜的（如成片导出）单独一组
  const groups = useMemo(() => {
    const buckets = new Map<string, { key: string; label: string; sort: string; assets: MediaAsset[] }>()
    for (const asset of assets) {
      const info = asset.shot_id != null ? shots[asset.shot_id] : undefined
      const key = info ? `shot-${asset.shot_id}` : 'unlinked'
      if (!buckets.has(key)) {
        buckets.set(key, {
          key,
          label: info
            ? `${info.episode} · 镜头 ${info.order}${info.title ? ` · ${info.title}` : ''}`
            : '未关联分镜（项目级 / 成片）',
          sort: info ? info.sort : 'zzz',
          assets: [],
        })
      }
      buckets.get(key)!.assets.push(asset)
    }
    return [...buckets.values()].sort((a, b) => (a.sort < b.sort ? -1 : 1))
  }, [assets, shots])

  const handleDelete = async (id: number) => {
    try {
      await mediaApi.remove(id)
      message.success('素材已删除')
      setAssets((prev) => prev.filter((asset) => asset.id !== id))
    } catch (error) {
      message.error(describeError(error))
    }
  }

  const openSaveAsset = (asset: MediaAsset) => {
    setAssetTarget(asset)
    assetForm.setFieldsValue({ kind: 'actor', name: '', description: '' })
  }

  const submitSaveAsset = async () => {
    if (!assetTarget) return
    try {
      const values = await assetForm.validateFields()
      setAssetSaving(true)
      // 不传 prompt：后端自动取该图生成时用的提示词作为资产外观描述
      const created = await assetApi.fromMedia({
        media_id: assetTarget.id,
        kind: values.kind,
        name: values.name.trim(),
        description: values.description ?? null,
        project_id: projectId,
      })
      message.success(`已存为资产「${created.name}」，生成关键帧 / 视频时可关联复用`)
      setAssetTarget(null)
    } catch (error) {
      if ((error as any)?.errorFields) return
      message.error(describeError(error))
    } finally {
      setAssetSaving(false)
    }
  }

  const total = assets.length

  return (
    <div>
      <div className="page-head">
        <div>
          <Space size={10} align="center" style={{ marginBottom: 2 }}>
            <Button icon={<ArrowLeftOutlined />} size="small" onClick={() => navigate('/media')}>
              返回
            </Button>
            <h2 className="page-title" style={{ margin: 0 }}>
              {project?.title ?? '项目'} · 素材库
            </h2>
          </Space>
          <p className="page-sub">
            共 {total} 个素材，按分镜分组展示；{project ? `画幅 ${project.aspect_ratio} · ${project.episode_count} 集 · ${project.shot_count} 镜` : ''}
          </p>
        </div>
        <Space>
          <Segmented
            value={kind}
            onChange={(value) => setKind(value as string)}
            options={[
              { value: 'all', label: '全部' },
              { value: 'image', label: '图片' },
              { value: 'video', label: '视频' },
            ]}
          />
          <Button icon={<ReloadOutlined />} onClick={() => void load()}>
            刷新
          </Button>
        </Space>
      </div>

      {loading ? null : groups.length === 0 ? (
        <Card>
          <Empty description="该项目暂无素材，去项目里生成一些吧" />
        </Card>
      ) : (
        groups.map((group) => (
          <Card
            key={group.key}
            style={{ marginBottom: 16 }}
            title={group.label}
            extra={
              <Typography.Text type="secondary" style={{ fontSize: 12.5 }}>
                {group.assets.length} 个素材
              </Typography.Text>
            }
          >
            <Row gutter={[16, 16]}>
              {group.assets.map((asset) => (
                <Col key={asset.id} xs={24} sm={12} md={8} lg={6} xxl={4}>
                  <MediaCard
                    asset={asset}
                    onExpand={() => setPreview(asset)}
                    onDelete={() => handleDelete(asset.id)}
                    onSaveAsset={() => openSaveAsset(asset)}
                  />
                </Col>
              ))}
            </Row>
          </Card>
        ))
      )}

      {/* 放大播放视频素材：支持全屏与拖动进度 */}
      <Modal
        title={preview ? `${preview.provider ?? ''} ${preview.model ?? preview.filename}`.trim() : ''}
        open={!!preview}
        onCancel={() => setPreview(null)}
        footer={null}
        centered
        width={900}
        destroyOnClose
      >
        {preview && (
          <video
            src={mediaUrl(preview.url)}
            controls
            autoPlay
            style={{ width: '100%', maxHeight: '72vh', background: '#000', borderRadius: 8 }}
          />
        )}
      </Modal>

      {/* 存为资产：把满意的图沉淀为可复用的角色 / 场景 / 道具 / 服装 */}
      <Modal
        title="存为资产"
        open={!!assetTarget}
        onCancel={() => setAssetTarget(null)}
        onOk={() => void submitSaveAsset()}
        confirmLoading={assetSaving}
        okText="保存为资产"
        cancelText="取消"
        width="min(560px, 92vw)"
        destroyOnClose
      >
        {assetTarget && (
          <Space align="start" size={16} style={{ width: '100%' }}>
            <Image
              src={mediaUrl(assetTarget.url)}
              width={120}
              style={{ objectFit: 'cover', borderRadius: 8 }}
            />
            <Form form={assetForm} layout="vertical" requiredMark={false} style={{ flex: 1 }}>
              <Form.Item name="kind" label="资产类型">
                <Select
                  options={[
                    { value: 'actor', label: '角色（锁人物长相与服装）' },
                    { value: 'scene', label: '场景（锁环境与光线）' },
                    { value: 'prop', label: '道具（锁关键物件）' },
                    { value: 'costume', label: '服装（锁一套造型）' },
                  ]}
                />
              </Form.Item>
              <Form.Item name="name" label="名称" rules={[{ required: true, message: '请填写名称' }]}>
                <Input placeholder="如：林晚 / 老宅客厅" maxLength={200} />
              </Form.Item>
              <Form.Item name="description" label="备注（可选）">
                <Input placeholder="如：女主，第 1 集出场" />
              </Form.Item>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                保存后会自动带上这张图生成时用的提示词作为外观描述，之后生成关键帧 / 视频时勾选它即可保持一致。
              </Typography.Text>
            </Form>
          </Space>
        )}
      </Modal>
    </div>
  )
}

/** 未归属项目：智能剪辑成片 / 单独上传的素材，不属于任何项目也要能被找到。 */
function UnaffiliatedMedia() {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const [assets, setAssets] = useState<MediaAsset[]>([])
  const [loading, setLoading] = useState(true)
  const [preview, setPreview] = useState<MediaAsset | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setAssets(await mediaApi.list({ unlinked: true }))
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setLoading(false)
    }
  }, [message])

  useEffect(() => {
    void load()
  }, [load])

  const handleDelete = async (id: number) => {
    try {
      await mediaApi.remove(id)
      message.success('素材已删除')
      setAssets((prev) => prev.filter((asset) => asset.id !== id))
    } catch (error) {
      message.error(describeError(error))
    }
  }

  return (
    <div>
      <div className="page-head">
        <div>
          <Space size={10} align="center" style={{ marginBottom: 2 }}>
            <Button icon={<ArrowLeftOutlined />} size="small" onClick={() => navigate('/media')}>
              返回
            </Button>
            <h2 className="page-title" style={{ margin: 0 }}>
              未归属项目的素材
            </h2>
          </Space>
          <p className="page-sub">共 {assets.length} 个素材；智能剪辑成片与单独上传的文件都在这里。</p>
        </div>
        <Button icon={<ReloadOutlined />} onClick={() => void load()}>
          刷新
        </Button>
      </div>

      {loading ? null : assets.length === 0 ? (
        <Card>
          <Empty description="暂无未归属素材" />
        </Card>
      ) : (
        <Row gutter={[16, 16]}>
          {assets.map((asset) => (
            <Col key={asset.id} xs={24} sm={12} md={8} lg={6} xxl={4}>
              <MediaCard
                asset={asset}
                onExpand={() => setPreview(asset)}
                onDelete={() => handleDelete(asset.id)}
              />
            </Col>
          ))}
        </Row>
      )}

      <Modal
        title={preview ? `${preview.provider ?? ''} ${preview.model ?? preview.filename}`.trim() : ''}
        open={!!preview}
        onCancel={() => setPreview(null)}
        footer={null}
        centered
        width={900}
        destroyOnClose
      >
        {preview && (
          <video
            src={mediaUrl(preview.url)}
            controls
            autoPlay
            style={{ width: '100%', maxHeight: '72vh', background: '#000', borderRadius: 8 }}
          />
        )}
      </Modal>
    </div>
  )
}

export default function MediaPage() {
  const { projectId } = useParams<{ projectId?: string }>()
  if (projectId === 'unlinked') return <UnaffiliatedMedia />
  const parsed = Number(projectId)
  return projectId && Number.isFinite(parsed) ? <ProjectMedia projectId={parsed} /> : <MediaProjects />
}
