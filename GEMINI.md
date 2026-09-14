# GEMINI.md

@AGENTS.md

<!--
Gemini CLI 默认只读取 GEMINI.md（默认上下文文件名），不读 AGENTS.md。
上面一行是 Gemini CLI 的 Memory Import 语法，启动时会把 AGENTS.md 展开进上下文，
因此 Gemini CLI 与其它工具读到的是同一份指令，无需重复维护。

请不要删除上面那一行，也不要把 AGENTS.md 的内容复制到这里。

若想彻底不用本文件，在 ~/.gemini/settings.json 里改为：

  { "context": { "fileName": ["AGENTS.md"] } }

注意：不要写成 ["GEMINI.md", "AGENTS.md"]。该配置是「全部加载」语义，
而 GEMINI.md 内部又会导入 AGENTS.md，会导致同一份内容重复注入两遍。
这是个人全局配置，无法随仓库分发，故这里保留桥接文件。
-->
