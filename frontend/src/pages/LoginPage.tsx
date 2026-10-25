import { useState } from 'react'
import { App, Button, Form, Input, Tabs, Typography } from 'antd'
import { RocketOutlined } from '@ant-design/icons'
import { Navigate, useNavigate } from 'react-router-dom'

import { useAuth } from '@/auth/AuthContext'
import { describeError } from '@/api/client'

interface FormValues {
  username: string
  password: string
  email?: string
  invite_code?: string
}

export default function LoginPage() {
  const { user, login, register } = useAuth()
  const { message } = App.useApp()
  const navigate = useNavigate()
  const [tab, setTab] = useState('login')
  const [loading, setLoading] = useState(false)

  if (user) return <Navigate to="/projects" replace />

  const handleSubmit = async (values: FormValues) => {
    setLoading(true)
    try {
      if (tab === 'login') {
        await login(values.username, values.password)
      } else {
        await register(
          values.username,
          values.password,
          values.email || undefined,
          values.invite_code?.trim() || undefined,
        )
      }
      message.success(tab === 'login' ? '欢迎回来' : '注册成功，已为你自动登录')
      navigate('/projects')
    } catch (error) {
      message.error(describeError(error))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="login-wrap">
      <div className="login-hero">
        <div>
          <Typography.Title level={4} style={{ color: '#fff', marginBottom: 28, fontWeight: 600 }}>
            AI 影视工坊
          </Typography.Title>
          <h1>从一句剧本，到一部短剧</h1>
          <p>
            接入你自己的大模型与影像 API，剧本拆解、分镜关键帧、图生视频、镜头合成一站式完成，
            让你的创意真正跑起来。
          </p>
        </div>
        <div className="login-points">
          <span>
            <RocketOutlined /> 自带 API Key，支持 DeepSeek / Agnes / Magic Hour 等多家厂商
          </span>
          <span>
            <RocketOutlined /> 剧本一键拆解为分镜，逐镜生成关键帧与视频
          </span>
          <span>
            <RocketOutlined /> FFmpeg 自动拼接镜头，一键导出成片
          </span>
        </div>
      </div>

      <div className="login-panel">
        <div className="login-card">
          <Typography.Title level={3} style={{ marginBottom: 6 }}>
            {tab === 'login' ? '登录账号' : '创建账号'}
          </Typography.Title>
          <Typography.Paragraph type="secondary" style={{ marginBottom: 22 }}>
            使用本地账号即可开始，数据与密钥都保存在你自己的环境里。
          </Typography.Paragraph>

          <Tabs
            activeKey={tab}
            onChange={setTab}
            items={[
              { key: 'login', label: '登录' },
              { key: 'register', label: '注册' },
            ]}
          />

          <Form<FormValues> layout="vertical" onFinish={handleSubmit} requiredMark={false} style={{ marginTop: 8 }}>
            <Form.Item
              name="username"
              label="用户名"
              rules={[
                { required: true, message: '请输入用户名' },
                { min: 3, message: '用户名至少 3 个字符' },
              ]}
            >
              <Input placeholder="例如 director" size="large" autoComplete="username" />
            </Form.Item>
            {tab === 'register' && (
              <>
                <Form.Item name="email" label="邮箱（可选）">
                  <Input placeholder="you@example.com" size="large" />
                </Form.Item>
                <Form.Item name="invite_code" label="邀请码（部署方启用时需要）">
                  <Input placeholder="没有启用邀请码可留空" size="large" />
                </Form.Item>
              </>
            )}
            <Form.Item
              name="password"
              label="密码"
              rules={[
                { required: true, message: '请输入密码' },
                { min: 6, message: '密码至少 6 位' },
              ]}
            >
              <Input.Password placeholder="至少 6 位" size="large" autoComplete="current-password" />
            </Form.Item>
            <Button type="primary" htmlType="submit" size="large" block loading={loading} style={{ marginTop: 4 }}>
              {tab === 'login' ? '登录' : '注册并登录'}
            </Button>
          </Form>
        </div>
      </div>
    </div>
  )
}
