/**
 * 项目版本 feature 前端 model(展示层形态, 与后端 DTO 解耦)。
 *
 * 版本主体直接复用共享 ProjectVersion(已与 version_to_dict 一一对应);
 * 本文件只定义创建表单 model 与提交守卫状态, 不复制后端字段。
 */

import type { ProjectVersion } from '../../types'

/** 版本历史展示 model: 即共享 ProjectVersion(字段含义见 types 注释)。 */
export type VersionModel = ProjectVersion

/** 创建版本表单 model(名称必填, 描述可选; 原因恒为 manual_save, 不由用户选择)。 */
export interface VersionCreateForm {
  name: string
  description: string
}

/** 详情加载状态(idle 未选 / loading / loaded / failed)。 */
export type VersionDetailState =
  | { status: 'idle' }
  | { status: 'loading'; versionId: number }
  | { status: 'loaded'; version: VersionModel }
  | { status: 'failed'; versionId: number; message: string }
