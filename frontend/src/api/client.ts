/**
 * 现有业务 API 的组合入口；各业务族逐步迁入 feature。
 * 通用传输、会话和下载辅助由 shared/http 拥有。
 * 结果/导出直接组合 feature 公开入口和 application 用例；
 * 尚未迁移的业务适配辅助不能作为新 feature 的依赖。
 */

import type {
  ApiErrorBody,
  Currency,
  ExcelExportInput,
  PackageExportInput,
  TaskStatus,
  TaskType,
} from '../types'
import {
  getResult,
  listAssessments,
  assess as assessResult,
  select as selectResult,
  readHourly,
} from '../features/results'
import {
  exportPackage,
  downloadExport,
} from '../features/exports'
import { exportTaskReport } from '../application/exportTaskReport'



export interface ExportsApi {
  /**
   * Excel 结果报告导出: 返回本地下载会话 id(一次下载引用, 非持久化报告)。
   * 项目包是完整项目现场导出, 不是报告, 同样只返回本地下载会话 id。
   */
  excel(input: ExcelExportInput): Promise<number>
  package(input: PackageExportInput): Promise<number>
  /** 按本地下载会话 id 取 Blob 与建议文件名并下载。 */
  download(downloadSessionId: number): Promise<{ blob: Blob; filename: string }>
}

export const api = {
  // 项目请求归 features/projects、features/project-versions 与
  // features/project-import 公开入口所有, 本聚合器不再保留项目委托。
  // 建模请求归 features/modeling 公开入口所有, 本聚合器不再保留委托。

  // 数据集请求归 features/datasets 公开入口所有, 本聚合器不再保留委托。


  // 计算配置请求归 features/config 所有(页面直接消费公开入口), 本聚合器不再保留委托。

  // 部署公开初值归 features/config 公开入口所有, 本聚合器不再保留委托。


  // 校验请求归 features/validation 所有(页面直接消费公开入口), 本聚合器不再保留委托。

  // 任务请求归 features/tasks 公开入口所有(页面直接消费公开入口), 本聚合器不再保留委托。

  results: {
    result: getResult,
    assessments: listAssessments,
    assess: assessResult,
    select: selectResult,
    hourly: readHourly,
  },

  exports: {
    excel: exportTaskReport,
    package: exportPackage,
    download: downloadExport,
  } satisfies ExportsApi,
}

// ---------------------------------------------------------------------------

export type { ApiErrorBody }

/** 任务类型/状态等枚举导出,供页面组件使用。 */
export type { TaskStatus, TaskType, Currency }
