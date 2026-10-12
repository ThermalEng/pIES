/**
 * 逐时单字段分页拼接(R5/R6 适配层)。
 *
 * 按公开 read_hourly 契约消费可信 DTO: 以 `next_start` 为空读到末页为止,
 * 拼接各页 `values`。不做长度比对/hash/循环防御/备用字段: 翻页进度由后端
 * `next_start` 保证(每页至少前进 1 行, 读完返回 null)。
 */
export async function collectHourlyValues(
  readPage: (start: number, limit: number) => Promise<{ values: number[]; next_start: number | null }>,
  rows: number,
): Promise<number[]> {
  const out: number[] = []
  let start = 0
  for (;;) {
    const page = await readPage(start, Math.max(rows - start, 1))
    for (const v of page.values) out.push(v)
    if (page.next_start === null) break
    start = page.next_start
  }
  return out
}
