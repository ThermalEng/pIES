/**
 * useYamlForm: 直接 YAML 编辑表单状态(未提交输入)。
 */

import { useCallback, useState } from 'react'

export interface YamlFormController {
  yaml_text: string
  touched: boolean
  setYaml: (text: string) => void
  reset: () => void
}

export function useYamlForm(initialYaml: string): YamlFormController {
  const [yaml_text, setYamlText] = useState(initialYaml)
  const [touched, setTouched] = useState(false)

  const setYaml = useCallback((text: string) => {
    setYamlText(text)
    setTouched(true)
  }, [])

  const reset = useCallback(() => {
    setYamlText(initialYaml)
    setTouched(false)
  }, [initialYaml])

  return { yaml_text, touched, setYaml, reset }
}
