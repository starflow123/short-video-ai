import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { Spin } from 'antd'
import type { ReactNode } from 'react'

import { useAuth } from '@/auth/AuthContext'
import AppLayout from '@/layouts/AppLayout'
import LoginPage from '@/pages/LoginPage'
import ProjectsPage from '@/pages/ProjectsPage'
import ProjectDetailPage from '@/pages/ProjectDetailPage'
import ShotDetailPage from '@/pages/ShotDetailPage'
import CredentialsPage from '@/pages/CredentialsPage'
import TasksPage from '@/pages/TasksPage'
import MediaPage from '@/pages/MediaPage'
import AssetsPage from '@/pages/AssetsPage'
import PromptsPage from '@/pages/PromptsPage'
import SmartEditPage from '@/pages/SmartEditPage'
import InsightsPage from '@/pages/InsightsPage'

function Protected({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth()
  const location = useLocation()

  if (loading) {
    return (
      <div className="boot-screen">
        <Spin size="large" />
      </div>
    )
  }
  if (!user) {
    return <Navigate to="/login" state={{ from: location.pathname }} replace />
  }
  return <>{children}</>
}

export default function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        element={
          <Protected>
            <AppLayout />
          </Protected>
        }
      >
        <Route path="/" element={<Navigate to="/projects" replace />} />
        <Route path="/projects" element={<ProjectsPage />} />
        <Route path="/projects/:projectId" element={<ProjectDetailPage />} />
        <Route path="/projects/:projectId/shots/:shotId" element={<ShotDetailPage />} />
        <Route path="/credentials" element={<CredentialsPage />} />
        <Route path="/tasks" element={<TasksPage />} />
        {/* /tasks/orphan 必须交给动态路由处理：走独立的静态路由时 useParams 拿不到
            projectId，TasksPage 里的孤儿任务分支永远进不去，点了只会留在项目卡片视图 */}
        <Route path="/tasks/:projectId" element={<TasksPage />} />
        <Route path="/insights" element={<InsightsPage />} />
        <Route path="/edit" element={<SmartEditPage />} />
        <Route path="/media" element={<MediaPage />} />
        <Route path="/media/:projectId" element={<MediaPage />} />
        <Route path="/assets" element={<AssetsPage />} />
        <Route path="/prompts" element={<PromptsPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/projects" replace />} />
    </Routes>
  )
}
