import { Card, Col, Empty, Row, Skeleton, Tag, Typography } from 'antd'
import { VideoCameraOutlined } from '@ant-design/icons'
import type { ReactNode } from 'react'

import type { Project } from '@/api/types'

const STATUS_LABEL: Record<string, { text: string; color: string }> = {
  draft: { text: '草稿', color: 'default' },
  storyboarded: { text: '已分镜', color: 'blue' },
  producing: { text: '生成中', color: 'processing' },
  completed: { text: '已完成', color: 'success' },
}

interface Props {
  projects: Project[]
  loading?: boolean
  onOpen: (id: number) => void
  /** 卡片底部额外统计项，如素材库的「图片 3 / 视频 2」 */
  stats?: (project: Project) => { label: string; value: ReactNode }[]
  emptyText?: string
}

/** 项目卡片网格：素材库 / 任务中心的一级入口，点卡片进入该项目的详情。 */
export default function ProjectCards({ projects, loading, onOpen, stats, emptyText = '还没有项目' }: Props) {
  if (loading) {
    return (
      <Row gutter={[16, 16]}>
        {[0, 1, 2].map((i) => (
          <Col key={i} xs={24} sm={12} lg={8} xxl={6}>
            <Card>
              <Skeleton active paragraph={{ rows: 2 }} />
            </Card>
          </Col>
        ))}
      </Row>
    )
  }

  if (projects.length === 0) {
    return (
      <Card>
        <Empty description={emptyText} />
      </Card>
    )
  }

  return (
    <Row gutter={[16, 16]}>
      {projects.map((project) => {
        const status = STATUS_LABEL[project.status] ?? { text: project.status, color: 'default' }
        const extraStats = stats?.(project) ?? []
        return (
          <Col key={project.id} xs={24} sm={12} lg={8} xxl={6}>
            <Card
              className="hover-card"
              hoverable
              title={
                <span
                  style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}
                  onClick={() => onOpen(project.id)}
                >
                  <VideoCameraOutlined style={{ color: '#4c6ef5' }} />
                  <span
                    style={{
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                      maxWidth: 150,
                    }}
                  >
                    {project.title}
                  </span>
                </span>
              }
              extra={<Tag color={status.color}>{status.text}</Tag>}
            >
              <div className="card-hit" onClick={() => onOpen(project.id)}>
                <Typography.Paragraph
                  type="secondary"
                  ellipsis={{ rows: 2 }}
                  style={{ minHeight: 44, marginBottom: 10 }}
                >
                  {project.description || '暂无简介'}
                </Typography.Paragraph>
                <div
                  className="muted"
                  style={{ display: 'flex', gap: 14, flexWrap: 'wrap', fontSize: 12.5 }}
                >
                  <span>画幅 {project.aspect_ratio}</span>
                  <span>{project.episode_count} 集</span>
                  <span>{project.shot_count} 镜</span>
                  {extraStats.map((item) => (
                    <span key={item.label}>
                      {item.label} {item.value}
                    </span>
                  ))}
                </div>
              </div>
            </Card>
          </Col>
        )
      })}
    </Row>
  )
}
