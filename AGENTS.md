1.只允许使用docker环境编译和测试，不要污染主机环境。在需要时再调用和测试。
2.开发过程中，需要较强模型的工作可交给codex完成，比如review，其他普通工作claude自己完成。
3.不允许使用本机其他文件夹的信息作为信息输入，不允许编辑除了/tmp文件夹和本文件夹外的其他内容。
4.设计、开发、重构和审查必须先阅读并遵守 `manual/developer-guide/zh-CN/ARCHITECTURE_CONSTITUTION.md`；与开发过程文档、现有代码或兼容行为冲突时，以该宪法规定的效力顺序裁决。要修改宪法前，必须征得用户同意；不得静默修改。
5.画布拖放功能的核查，由人工进行，不要用playwright；画布组件的其他功能，如参数设定等，仍由playwright执行。
6.完成一个独立功能，review通过后就git提交，各子agent工作在独立工作树，避免丢失历史状态，提高合作效率。
7.docker测试完成后，应及时清理测试生成的image（不包含基础镜像），防止占用磁盘空间。
8.开发“模型与算法”、用户模型库、算法插件、共享园地或相应选择器前，必须先阅读 `manual/developer-guide/zh-CN/customization-center.md` 和 `docs/development/model-and-algorithm-ai-requirements.md`；实现必须按其中的切片顺序推进，不得把上传包运行期热加载到 API 或 Worker 进程。
9.涉及到较复杂的开发工作，尽量使用herdr拉起claude让子进程完成，简单的修改合并可以自己完成。herdr自动化指南：https://raw.githubusercontent.com/herdrdev/herdr/v0.8.2/docs/next/website/src/content/docs/agent-automation.mdx
10.只实现当前明确的核心业务逻辑，不得针对未提出、无证据的攻击面或故障模式增加假想安全加固、备用分支或防御性代码；及时删除未使用的防御代码，避免过度工程。可信流程内的 SHA-256/hash 校验必须全部删除：对程序本地生成并在本地继续使用的文件、数据、中间产物和派生配置，不得额外重算、比对或自校验 SHA-256/hash，并删除相应指导文字、代码与测试。只有在不可替代的外部边界完整性、对象存储内容寻址或现行公开契约的精确内容身份确实需要 hash 时才保留；保留理由必须能指向具体边界或契约，不得以泛化的“安全”或“防篡改”为由。
