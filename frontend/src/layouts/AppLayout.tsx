import { useEffect, useState } from 'react'
import {
  AppstoreOutlined,
  DashboardOutlined,
  FileTextOutlined,
  KeyOutlined,
  MenuFoldOutlined,
  MenuOutlined,
  PictureOutlined,
  ScissorOutlined,
  TeamOutlined,
} from '@ant-design/icons'
import { Avatar, Dropdown, Layout, Menu, Typography } from 'antd'
import { Link, Outlet, useLocation, useNavigate } from 'react-router-dom'

import { useAuth } from '@/auth/AuthContext'
import TaskProgressDrawer from '@/components/TaskProgressDrawer'

const { Sider, Header, Content } = Layout

/** 窗口窄于该宽度时侧栏自动收起，避免 216px 的侧栏挤掉大部分内容区导致页面被横向截断 */
const NARROW_WIDTH = 992

const NAV = [
  { key: '/projects', icon: <AppstoreOutlined />, label: '我的项目' },
  { key: '/edit', icon: <ScissorOutlined />, label: '智能剪辑' },
  { key: '/assets', icon: <TeamOutlined />, label: '资产管理' },
  { key: '/prompts', icon: <FileTextOutlined />, label: '提示词模板' },
  { key: '/credentials', icon: <KeyOutlined />, label: '密钥管理' },
  { key: '/tasks', icon: <DashboardOutlined />, label: '任务中心' },
  { key: '/media', icon: <PictureOutlined />, label: '素材库' },
]

export default function AppLayout() {
  const location = useLocation()
  const navigate = useNavigate()
  const { user, logout } = useAuth()

  // 自己根据窗口宽度控制侧栏折叠（不依赖 antd 的 matchMedia 响应式，行为更确定）
  const [narrow, setNarrow] = useState(() => window.innerWidth < NARROW_WIDTH)
  const [collapsed, setCollapsed] = useState(() => window.innerWidth < NARROW_WIDTH)

  useEffect(() => {
    const onResize = () => {
      const isNarrow = window.innerWidth < NARROW_WIDTH
      setNarrow(isNarrow)
      if (isNarrow) setCollapsed(true)
    }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  const selected = NAV.find((item) => location.pathname.startsWith(item.key))?.key ?? '/projects'

  return (
    <Layout className="app-shell">
      <Sider
        width={216}
        className="app-sider"
        theme="light"
        collapsed={collapsed}
        collapsedWidth={0}
        trigger={null}
      >
        <Link to="/projects" className="brand">
          <span className="brand-mark">AI</span>
          <span className="brand-text">
            <strong>AI 影视工坊</strong>
            <small>Video Studio</small>
          </span>
        </Link>
        <Menu
          mode="inline"
          selectedKeys={[selected]}
          items={NAV.map(({ key, icon, label }) => ({ key, icon, label }))}
          onClick={({ key }) => navigate(key)}
        />
      </Sider>
      <Layout>
        <Header className="app-header">
          <div className="header-left">
            {narrow && (
              <button
                type="button"
                className="sider-toggle"
                onClick={() => setCollapsed((value) => !value)}
                title={collapsed ? '展开导航' : '收起导航'}
              >
                {collapsed ? <MenuOutlined /> : <MenuFoldOutlined />}
              </button>
            )}
            <Typography.Text type="secondary">
              自带 Key · 多模型驱动 · 从剧本到成片
            </Typography.Text>
          </div>
          <Dropdown
            menu={{
              items: [{ key: 'logout', label: '退出登录' }],
              onClick: ({ key }) => {
                if (key === 'logout') {
                  logout()
                  navigate('/login')
                }
              },
            }}
          >
            <div className="user-chip">
              <Avatar size={28} style={{ background: '#4c6ef5' }}>
                {user?.username?.[0]?.toUpperCase() ?? 'U'}
              </Avatar>
              <span>{user?.username}</span>
            </div>
          </Dropdown>
        </Header>
        <Content className="app-content">
          <Outlet />
        </Content>
      </Layout>
      <TaskProgressDrawer />
    </Layout>
  )
}
