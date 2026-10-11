/**
 * 项目版本 feature 前后端契约(与后端 HTTP JSON 字段一一对应, snake_case)。
 *
 * 端点(与后端 /api 路由一致):
 *   POST /api/projects/{pid}/versions          从当前草稿创建版本 → {version: {...}}
 *   GET  /api/projects/{pid}/versions          版本列表(新版本在前) → {versions: [...]}
 *   GET  /api/projects/{pid}/versions/{vid}    版本详情(仅版本元数据) → {version: {...}}
 *
 * 创建原因固定 `manual_save`: 本切片不提供可伪装 restore/apply_result 的
 * 任意原因选择; 快照自动固化(snapshot_freeze)由后端任务链写入, 前端不发送。
 * 详情端点只返回版本元数据, 不读取完整模型内容, 详情页不得声称展示完整模型。
 *
 * 版本主体类型以共享 ProjectVersion 为唯一实际类型, 本文件只定义创建请求
 * DTO, 不保留与之平行的响应 DTO。
 *
 * 依赖方向: features/project-versions → ../../types(共享类型)/../../api/client(HTTP 层)。
 */

/** 创建版本请求(与后端 VersionCreateRequest 对应)。 */
export interface VersionCreateRequestDto {
  /** 版本名称(后端 min_length=1, max_length=200; 空串由后端拒收)。 */
  name: string
  /** 版本描述(可空, 后端 max_length=2000)。 */
  description: string | null
  /** 创建原因: 本切片恒为 manual_save。 */
  reason: 'manual_save'
}

