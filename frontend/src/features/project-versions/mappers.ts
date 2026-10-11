/**
 * 项目版本 feature mapper: 表单 model → 请求 DTO(纯函数)。
 *
 * 名称两侧空白在前端收敛一次(后端仍以 PROJ-CMD-001 为权威拒收空名);
 * 描述空串归一为 null(与后端 Optional 语义一致); reason 恒为 manual_save,
 * 不接受调用方传入(防止伪装 restore/apply_result 等后端内部原因)。
 * 响应 DTO → VersionModel 的严格解析在 api/client 完成, 此处不重复。
 */

import type { VersionCreateRequestDto } from './contracts'
import type { VersionCreateForm } from './model'

/** 创建表单校验失败(纯前端必填检查, 后端仍为权威)。 */
export class VersionFormError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'VersionFormError'
  }
}

/**
 * 创建表单 → 请求 DTO。
 * 名称去首尾空白后仍为空则抛 VersionFormError(阻止发送, 保留用户输入)。
 */
export function buildCreateRequest(form: VersionCreateForm): VersionCreateRequestDto {
  const name = form.name.trim()
  if (!name) {
    throw new VersionFormError('version name is required')
  }
  const description = form.description.trim()
  return {
    name,
    description: description ? description : null,
    reason: 'manual_save',
  }
}
